"""The LLM client end to end against the fake provider (no network, no real model)."""

from decimal import Decimal

import pytest
from pydantic import BaseModel

from apps.llm.client import call_structured, call_text, extract_json_object
from apps.llm.exceptions import (
    LLMConfigError,
    LLMOutputError,
    LLMProviderError,
    PromptError,
    QuotaExceeded,
)
from apps.llm.models import LLMCache, LLMCall, UsageQuota
from apps.llm.providers.fake import FakeProvider
from tests.factories import UserFactory
from tests.helpers import raises_detail
from tests.logcapture import ListHandler

pytestmark = pytest.mark.django_db


class Score(BaseModel):
    score: int
    reasons: list[str]


GOOD = '{"score": 80, "reasons": ["fits"]}'
PRICES = {"fake-fast": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}


def _score(user=None, **kwargs):  # type: ignore[no-untyped-def]
    return call_structured(
        task="score_match",
        prompt_name="score_test",
        variables={"job": "Data labeller"},
        schema=Score,
        user=user,
        **kwargs,
    )


# --- happy path and logging ------------------------------------------------------------------


def test_structured_call_returns_a_valid_object_and_logs_the_call(
    fake_llm: FakeProvider, prompts_dir, settings
) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_PRICES = PRICES
    fake_llm.script.append(GOOD)
    user = UserFactory()

    result = _score(user)

    assert result == Score(score=80, reasons=["fits"])
    call = LLMCall.objects.get()
    assert (call.user, call.task, call.provider, call.model) == (
        user,
        "score_match",
        "fake",
        "fake-fast",
    )
    assert call.success and not call.cache_hit and call.error == ""
    assert call.prompt_version == "3"
    assert call.input_tokens > 0 and call.output_tokens > 0
    expected = (Decimal(call.input_tokens) * 1 + Decimal(call.output_tokens) * 2) / 1_000_000
    assert call.cost_usd == expected.quantize(Decimal("0.000001"))
    assert call.latency_ms >= 0


def test_provider_receives_prompt_safety_notice_and_schema(
    fake_llm: FakeProvider, prompts_dir
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(GOOD)
    _score()
    sent = fake_llm.calls[0]
    assert sent["system"].startswith("You score jobs.")
    assert "untrusted data" in sent["system"]
    assert '"score"' in sent["system"]  # JSON schema included
    assert sent["messages"] == [{"role": "user", "content": "Score this: Data labeller"}]
    assert sent["task"] == "score_match" and sent["schema"] is Score


def test_tier_comes_from_the_prompt_unless_overridden(
    fake_llm: FakeProvider, prompts_dir, settings
) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_MODEL_FAST, settings.LLM_MODEL_WRITER = "fast-m", "writer-m"
    fake_llm.script.extend([GOOD, GOOD])
    _score()
    _score(model_tier="writer")
    assert [c["model"] for c in fake_llm.calls] == ["fast-m", "writer-m"]


def test_system_calls_have_no_user(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(GOOD)
    _score(user=None)
    assert LLMCall.objects.get().user is None


def test_json_in_code_fences_or_prose_is_accepted(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend([f"```json\n{GOOD}\n```", f"Sure! Here you go: {GOOD} Hope it helps."])
    assert _score().score == 80
    assert _score().score == 80
    assert len(fake_llm.calls) == 2  # no repair needed


@pytest.mark.parametrize("text", ["no json here", "{broken", "[1, 2]"])
def test_extract_json_object_rejects_non_objects_gracefully(text: str) -> None:
    try:
        value = extract_json_object(text)
    except ValueError:
        return
    assert not isinstance(value, dict)


# --- validation and the repair retry ---------------------------------------------------------


def test_invalid_json_triggers_exactly_one_repair_retry(
    fake_llm: FakeProvider, prompts_dir, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["this is not json", GOOD])

    result = _score(UserFactory())

    assert result.score == 80
    assert len(fake_llm.calls) == 2
    repair_messages = fake_llm.calls[1]["messages"]
    assert repair_messages[-2] == {"role": "assistant", "content": "this is not json"}
    assert "not valid" in repair_messages[-1]["content"]
    assert handler.events("llm_repair_retry")
    call = LLMCall.objects.get()  # one logical call, tokens from both attempts
    assert call.success
    assert call.input_tokens > fake_llm.calls[0]["system"].__len__() // 8


def test_schema_violation_is_repaired_with_a_message_naming_the_field(
    fake_llm: FakeProvider, prompts_dir
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(['{"score": "high", "reasons": []}', GOOD])
    _score()
    assert "score" in fake_llm.calls[1]["messages"][-1]["content"]


def test_two_failures_raise_llm_output_error_with_no_third_attempt(
    fake_llm: FakeProvider, prompts_dir, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["nope", '{"score": "SECRET-VALUE-123"}'])

    with pytest.raises(LLMOutputError):
        _score(UserFactory())

    assert len(fake_llm.calls) == 2  # original + exactly one repair
    call = LLMCall.objects.get()
    assert call.success is False
    assert call.error.startswith("schema_invalid:") and "score" in call.error
    assert "SECRET-VALUE-123" not in call.error
    assert "SECRET-VALUE-123" not in "".join(handler.lines)
    (line,) = handler.events("llm_call")
    assert line["level"] == "ERROR" and line["success"] is False


def test_empty_text_response_is_an_output_error(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append("   ")
    with pytest.raises(LLMOutputError):
        call_text(
            task="generate_cover_letter",
            prompt_name="letter_test",
            variables={"job": "x"},
            user=None,
        )
    assert LLMCall.objects.get().error == "empty_response"


def test_provider_errors_are_logged_and_reraised(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(LLMProviderError("http_503:overloaded"))
    with pytest.raises(LLMProviderError):
        _score(UserFactory())
    call = LLMCall.objects.get()
    assert not call.success and "http_503" in call.error


def test_unexpected_errors_are_logged_without_their_message(
    fake_llm: FakeProvider, prompts_dir
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(RuntimeError("contains CV text"))
    with pytest.raises(RuntimeError):
        _score()
    assert LLMCall.objects.get().error == "unexpected: RuntimeError"


# --- text calls ------------------------------------------------------------------------------


def test_call_text_returns_stripped_text_on_the_writer_tier(
    fake_llm: FakeProvider, prompts_dir, settings
) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_MODEL_WRITER = "writer-m"
    fake_llm.script.append("  Dear team,\nHello.  ")
    text = call_text(
        task="generate_cover_letter",
        prompt_name="letter_test",
        variables={"job": "x"},
        user=UserFactory(),
    )
    assert text == "Dear team,\nHello."
    assert fake_llm.calls[0]["model"] == "writer-m"
    assert fake_llm.calls[0]["schema"] is None


# --- prompt / config problems ----------------------------------------------------------------


def test_prompt_for_a_different_task_is_rejected(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    with raises_detail(PromptError, "task"):
        call_structured(
            task="parse_cv",
            prompt_name="score_test",
            variables={"job": "x"},
            schema=Score,
            user=None,
        )
    assert fake_llm.calls == [] and LLMCall.objects.count() == 0


def test_missing_variable_fails_before_any_provider_call(
    fake_llm: FakeProvider, prompts_dir
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PromptError):
        call_structured(
            task="score_match", prompt_name="score_test", variables={}, schema=Score, user=None
        )
    assert fake_llm.calls == []


def test_real_provider_without_a_model_is_a_config_error(prompts_dir, settings) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_PROVIDER, settings.LLM_API_KEY, settings.LLM_MODEL_FAST = "openai_compat", "k", ""
    with raises_detail(LLMConfigError, "LLM_MODEL_FAST"):
        _score()
    assert LLMCall.objects.count() == 0


# --- quotas ----------------------------------------------------------------------------------


def test_quota_exceeded_is_raised_before_any_provider_call(
    fake_llm: FakeProvider, prompts_dir, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=0)

    with pytest.raises(QuotaExceeded) as info:
        _score(user)

    assert info.value.scope == "scores"
    assert fake_llm.calls == []
    assert LLMCall.objects.count() == 0
    assert handler.events("quota_exceeded")


def test_user_quota_blocks_after_the_limit_is_reached(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=2)
    fake_llm.script.extend([GOOD, GOOD])
    _score(user)
    _score(user)
    with pytest.raises(QuotaExceeded):
        _score(user)
    assert len(fake_llm.calls) == 2


def test_system_daily_cap_blocks_system_calls(
    fake_llm: FakeProvider, prompts_dir, settings
) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_SYSTEM_DAILY_USD_CAP = "0.50"
    LLMCall.objects.create(
        user=None, task="extract_job", provider="x", model="m", cost_usd=Decimal("0.5")
    )
    with pytest.raises(QuotaExceeded) as info:
        _score(user=None)
    assert info.value.scope == "system_daily" and fake_llm.calls == []


# --- cache -----------------------------------------------------------------------------------


def test_cache_returns_the_stored_result_without_calling_the_provider(
    fake_llm: FakeProvider, prompts_dir
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(GOOD)
    first = _score(cache_key="job:1")
    second = _score(cache_key="job:1")

    assert first == second
    assert len(fake_llm.calls) == 1
    hit = LLMCall.objects.get(cache_hit=True)
    assert hit.success and hit.cost_usd == 0 and hit.input_tokens == 0
    assert LLMCache.objects.get().key == "job:1"


def test_cache_hits_do_not_use_quota(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=1)
    fake_llm.script.append(GOOD)
    _score(user, cache_key="k")
    assert _score(user, cache_key="k").score == 80  # quota is spent, but the cache still answers


def test_a_new_prompt_version_does_not_reuse_the_old_cache(
    fake_llm: FakeProvider, prompts_dir
) -> None:  # type: ignore[no-untyped-def]
    from apps.llm.prompts import clear_prompt_cache

    fake_llm.script.extend([GOOD, '{"score": 10, "reasons": []}'])
    _score(cache_key="job:1")
    path = prompts_dir / "score_test.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("version: 3", "version: 4"), encoding="utf-8"
    )
    clear_prompt_cache()

    assert _score(cache_key="job:1").score == 10
    assert len(fake_llm.calls) == 2
    assert LLMCache.objects.count() == 2


def test_a_cached_value_that_no_longer_fits_the_schema_is_ignored(
    fake_llm: FakeProvider, prompts_dir
) -> None:  # type: ignore[no-untyped-def]
    LLMCache.objects.create(key="job:1", prompt_version="3", result={"unexpected": True})
    fake_llm.script.append(GOOD)
    assert _score(cache_key="job:1").score == 80
    assert len(fake_llm.calls) == 1
    assert LLMCache.objects.get().result["score"] == 80  # overwritten with the fresh result


def test_text_results_are_cached_too(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append("A letter.")
    args = {
        "task": "generate_cover_letter",
        "prompt_name": "letter_test",
        "variables": {"job": "x"},
        "user": None,
    }
    assert call_text(**args, cache_key="letter:1") == "A letter."
    assert call_text(**args, cache_key="letter:1") == "A letter."
    assert len(fake_llm.calls) == 1


def test_failures_are_not_cached(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["bad", "worse"])
    with pytest.raises(LLMOutputError):
        _score(cache_key="job:1")
    assert LLMCache.objects.count() == 0


# --- what is (not) stored and logged ---------------------------------------------------------


def test_bodies_are_not_stored_by_default(fake_llm: FakeProvider, prompts_dir) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(GOOD)
    _score()
    call = LLMCall.objects.get()
    assert call.prompt_body == "" and call.response_body == ""


def test_bodies_are_stored_only_when_enabled(fake_llm: FakeProvider, prompts_dir, settings) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_LOG_BODIES = True
    fake_llm.script.append(GOOD)
    _score()
    call = LLMCall.objects.get()
    assert "Data labeller" in call.prompt_body and call.response_body == GOOD


def test_logs_never_contain_prompt_or_response_text(
    fake_llm: FakeProvider, prompts_dir, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append('{"score": 1, "reasons": ["PRIVATE-RESPONSE-TEXT"]}')
    call_structured(
        task="score_match", prompt_name="score_test", variables={"job": "PRIVATE-PROMPT-TEXT"},
        schema=Score, user=UserFactory(),
    )  # fmt: skip
    output = "".join(handler.lines)
    assert "PRIVATE-PROMPT-TEXT" not in output and "PRIVATE-RESPONSE-TEXT" not in output
    (line,) = handler.events("llm_call")
    assert {
        "task",
        "model",
        "in_tok",
        "out_tok",
        "cost_usd",
        "latency_ms",
        "cache_hit",
        "success",
    } <= set(line)


def test_llm_call_log_shows_real_token_counts_not_redacted_values(
    fake_llm: FakeProvider, prompts_dir, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(GOOD)
    _score(UserFactory())
    (line,) = handler.events("llm_call")
    call = LLMCall.objects.get()
    assert line["in_tok"] == call.input_tokens > 0
    assert line["out_tok"] == call.output_tokens > 0
    assert line["for_user_id"] == call.user_id
