import json
from decimal import Decimal  # noqa: F401  (kept for readability of price-related asserts)
from typing import Literal

import httpx
import pytest
import respx
from pydantic import BaseModel

from apps.llm.exceptions import LLMConfigError, LLMProviderError
from apps.llm.providers import get_provider
from apps.llm.providers.fake import FakeProvider, default_for
from apps.llm.providers.openai_compat import OpenAICompatProvider
from apps.llm.schemas import SmokeResult
from tests.helpers import raises_detail

URL = "https://llm.example.test/v1/chat/completions"


class Inner(BaseModel):
    flag: bool
    n: int


class Everything(BaseModel):
    text: str
    count: int
    ratio: float
    ok: bool
    kind: Literal["remote", "onsite"]
    maybe: str | None
    tags: list[str]
    nested: Inner
    extra: dict[str, str]
    preset: int = 7


# --- fake provider ---------------------------------------------------------------------------


def test_defaults_are_schema_valid_for_every_field_kind() -> None:
    data = default_for(Everything)
    obj = Everything.model_validate(data)
    assert obj.kind == "remote" and obj.maybe is None and obj.tags == [] and obj.preset == 7


def test_fake_uses_fixture_file_for_the_task(fake_llm: FakeProvider) -> None:
    response = fake_llm.complete(
        model="m", system="s", messages=[], max_tokens=10, json_mode=True,
        task="smoke", schema=SmokeResult,
    )  # fmt: skip
    assert json.loads(response.text) == {"ok": True, "message": "pong"}
    assert response.input_tokens >= 1 and response.output_tokens >= 1


def test_fake_falls_back_to_schema_defaults(fake_llm: FakeProvider) -> None:
    response = fake_llm.complete(
        model="m", system="s", messages=[], max_tokens=10, json_mode=True,
        task="no_fixture_for_this", schema=SmokeResult,
    )  # fmt: skip
    assert SmokeResult.model_validate_json(response.text).ok is True


def test_fake_text_fallback(fake_llm: FakeProvider) -> None:
    response = fake_llm.complete(
        model="m", system="s", messages=[], max_tokens=10, json_mode=False, task="x"
    )
    assert "placeholder" in response.text


def test_fake_script_returns_items_in_order_then_raises_exceptions(
    fake_llm: FakeProvider,
) -> None:
    fake_llm.script.extend(["first", RuntimeError("boom")])
    assert (
        fake_llm.complete(model="m", system="", messages=[], max_tokens=1, json_mode=False).text
        == "first"
    )
    with pytest.raises(RuntimeError):
        fake_llm.complete(model="m", system="", messages=[], max_tokens=1, json_mode=False)


def test_provider_selection_follows_settings(settings) -> None:  # type: ignore[no-untyped-def]
    assert get_provider().name == "fake"
    settings.LLM_PROVIDER = "openai_compat"
    settings.LLM_API_KEY = "k"
    assert get_provider().name == "openai_compat"


def test_unknown_provider_and_missing_key_are_config_errors(settings) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_PROVIDER = "mystery"
    with pytest.raises(LLMConfigError):
        get_provider()
    settings.LLM_PROVIDER = "openai_compat"
    settings.LLM_API_KEY = ""
    with pytest.raises(LLMConfigError):
        get_provider()


# --- openai_compat (HTTP mocked with respx) --------------------------------------------------


def _provider(sleeps: list[float] | None = None, **kwargs: object) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url="https://llm.example.test/v1/",
        api_key="sk-secret-key",
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        **kwargs,  # type: ignore[arg-type]
    )


def _ok(content: str = '{"ok": true}', usage: dict[str, int] | None = None) -> httpx.Response:
    body: dict[str, object] = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


@respx.mock
def test_request_shape_and_parsed_response() -> None:
    route = respx.post(URL).mock(
        return_value=_ok("hello", {"prompt_tokens": 12, "completion_tokens": 3})
    )
    response = _provider().complete(
        model="gpt-test", system="SYS", messages=[{"role": "user", "content": "hi"}],
        max_tokens=50, json_mode=True,
    )  # fmt: skip
    assert (response.text, response.input_tokens, response.output_tokens) == ("hello", 12, 3)

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-secret-key"
    sent = json.loads(request.content)
    assert sent["model"] == "gpt-test"
    assert sent["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
    ]
    assert sent["max_tokens"] == 50
    assert sent["response_format"] == {"type": "json_object"}


@respx.mock
def test_json_mode_can_be_disabled_and_token_param_renamed() -> None:
    route = respx.post(URL).mock(return_value=_ok())
    _provider(json_mode_supported=False, max_tokens_param="max_completion_tokens").complete(
        model="m", system="s", messages=[], max_tokens=5, json_mode=True
    )
    sent = json.loads(route.calls.last.request.content)
    assert "response_format" not in sent and sent["max_completion_tokens"] == 5
    assert "max_tokens" not in sent


@respx.mock
def test_text_calls_do_not_request_json_mode() -> None:
    route = respx.post(URL).mock(return_value=_ok("plain"))
    _provider().complete(model="m", system="s", messages=[], max_tokens=5, json_mode=False)
    assert "response_format" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_missing_usage_estimates_output_tokens() -> None:
    respx.post(URL).mock(return_value=_ok("x" * 40))
    response = _provider().complete(
        model="m", system="s", messages=[], max_tokens=5, json_mode=False
    )
    assert response.input_tokens == 0 and response.output_tokens == 10


@respx.mock
def test_content_parts_are_joined() -> None:
    body = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    assert (
        _provider().complete(model="m", system="s", messages=[], max_tokens=5, json_mode=False).text
        == "ab"
    )


@respx.mock
def test_retries_on_5xx_then_succeeds_with_backoff() -> None:
    sleeps: list[float] = []
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), _ok("fine")]
    )
    response = _provider(sleeps).complete(
        model="m", system="s", messages=[], max_tokens=5, json_mode=False
    )
    assert response.text == "fine" and route.call_count == 3
    assert sleeps == [1.0, 2.0]


@respx.mock
def test_retries_on_timeout() -> None:
    route = respx.post(URL).mock(side_effect=[httpx.ReadTimeout("slow"), _ok("fine")])
    response = _provider().complete(
        model="m", system="s", messages=[], max_tokens=5, json_mode=False
    )
    assert response.text == "fine" and route.call_count == 2


@respx.mock
def test_429_honours_retry_after_up_to_a_cap() -> None:
    sleeps: list[float] = []
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "3"}),
            httpx.Response(429, headers={"retry-after": "999"}),
            _ok("fine"),
        ]
    )
    _provider(sleeps).complete(model="m", system="s", messages=[], max_tokens=5, json_mode=False)
    assert sleeps == [3.0, 10.0]


@respx.mock
def test_gives_up_after_two_retries() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(502))
    with raises_detail(LLMProviderError, "http_502"):
        _provider().complete(model="m", system="s", messages=[], max_tokens=5, json_mode=False)
    assert route.call_count == 3


@respx.mock
def test_transport_errors_become_provider_errors() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    with raises_detail(LLMProviderError, "transport_error"):
        _provider().complete(model="m", system="s", messages=[], max_tokens=5, json_mode=False)


@respx.mock
def test_4xx_is_not_retried_and_error_carries_type_not_message() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"type": "invalid_request_error", "message": "echoed: SECRET PROMPT"}},
        )
    )
    with pytest.raises(LLMProviderError) as info:
        _provider().complete(model="m", system="s", messages=[], max_tokens=5, json_mode=False)
    assert route.call_count == 1
    assert info.value.args[0] == "http_400:invalid_request_error"
    assert "SECRET" not in repr(info.value.args) and "sk-secret-key" not in repr(info.value.args)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    ],
)
@respx.mock
def test_malformed_success_responses_are_provider_errors(response: httpx.Response) -> None:
    respx.post(URL).mock(return_value=response)
    with raises_detail(LLMProviderError, "malformed_response"):
        _provider().complete(model="m", system="s", messages=[], max_tokens=5, json_mode=False)
