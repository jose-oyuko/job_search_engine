"""The single entry point for LLM calls (spec §11.1).

Order of work: load and render the prompt, answer from the cache if possible, check quotas,
call the provider, validate the output (one repair retry), log an `LLMCall`, store the cache.
Quota and configuration errors are raised before anything is sent to a provider.

Nothing here logs prompts, responses or user content; see `LLMCall` for what is stored.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from django.conf import settings
from pydantic import BaseModel, ValidationError

from apps.accounts.models import User
from apps.core.logging import log_event

from .exceptions import LLMConfigError, LLMError, LLMOutputError, PromptError
from .models import LLMCall
from .prompts import Prompt, load_prompt
from .providers import get_provider
from .providers.base import ProviderResponse
from .services import check_quota, compute_cost, get_cached, store_cached
from .untrusted import UNTRUSTED_NOTICE

logger = logging.getLogger(__name__)

Tier = Literal["fast", "writer"]

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_MAX_ERROR_CHARS = 1000


def extract_json_object(text: str) -> Any:
    """Parse the first JSON object in `text`, tolerating code fences and surrounding prose."""
    text = text.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


def _validation_summary(exc: Exception) -> str:
    """Field paths and generic messages only, never the offending values (they may be personal)."""
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        ]
        return "; ".join(parts)[:_MAX_ERROR_CHARS]
    return "the reply was not a valid JSON object"


def _failed_fields(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        fields = sorted({".".join(str(p) for p in err["loc"]) or "root" for err in exc.errors()})
        return ("schema_invalid: " + ", ".join(fields))[:300]
    return "invalid_json"


@dataclass
class _Tracker:
    """Collects what one logical call did, so a single `LLMCall` row can be written at the end."""

    task: str
    user: User | None
    provider: str
    model: str
    prompt_version: str
    started: float = field(default_factory=time.monotonic)
    input_tokens: int = 0
    output_tokens: int = 0
    success: bool = False
    error: str = ""
    cache_hit: bool = False
    prompt_body: str = ""
    response_body: str = ""

    def add(self, response: ProviderResponse) -> None:
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens

    def finish(self) -> LLMCall:
        cost = Decimal("0")
        if not self.cache_hit:
            cost = compute_cost(self.model, self.input_tokens, self.output_tokens)
        latency_ms = round((time.monotonic() - self.started) * 1000)
        call = LLMCall.objects.create(
            user=self.user,
            task=self.task,
            provider=self.provider,
            model=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            success=self.success,
            error=self.error[:300],
            cache_hit=self.cache_hit,
            prompt_version=self.prompt_version,
            prompt_body=self.prompt_body if settings.LLM_LOG_BODIES else "",
            response_body=self.response_body if settings.LLM_LOG_BODIES else "",
        )
        log_event(
            logger,
            logging.INFO if self.success else logging.ERROR,
            "llm_call",
            "LLM call finished" if self.success else "LLM call failed",
            call_id=call.pk,
            task=self.task,
            provider=self.provider,
            model=self.model,
            # Not "input_tokens": the redaction filter masks any key containing "token".
            in_tok=self.input_tokens,
            out_tok=self.output_tokens,
            cost_usd=str(cost),
            latency_ms=latency_ms,
            cache_hit=self.cache_hit,
            success=self.success,
            prompt_version=self.prompt_version,
            error=self.error,
            for_user_id=self.user.pk if self.user else None,
        )
        return call


def _resolve_model(tier: str) -> str:
    model: str = settings.LLM_MODEL_WRITER if tier == "writer" else settings.LLM_MODEL_FAST
    if model:
        return model
    if settings.LLM_PROVIDER == "fake":
        return f"fake-{tier}"
    raise LLMConfigError(f"LLM_MODEL_{tier.upper()} is not set")


def _build_system(prompt_system: str, schema: type[BaseModel] | None) -> str:
    parts = [prompt_system, UNTRUSTED_NOTICE]
    if schema is not None:
        parts.append(
            "Respond with a single JSON object and nothing else. It must match this JSON "
            "schema:\n" + json.dumps(schema.model_json_schema(), separators=(",", ":"))
        )
    return "\n\n".join(parts)


def _run(
    *,
    task: str,
    prompt_name: str,
    variables: dict[str, Any],
    schema: type[BaseModel] | None,
    user: User | None,
    model_tier: Tier | None,
    max_output_tokens: int,
    cache_key: str | None,
) -> str | BaseModel:
    prompt: Prompt = load_prompt(prompt_name)
    if prompt.task != task:
        raise PromptError(f"prompt {prompt_name!r} is for task {prompt.task!r}, not {task!r}")
    tier = model_tier or prompt.tier
    model = _resolve_model(tier)
    system_text, user_text = prompt.render(variables)
    system = _build_system(system_text, schema)

    tracker = _Tracker(
        task=task,
        user=user,
        provider=settings.LLM_PROVIDER,
        model=model,
        prompt_version=prompt.version,
        prompt_body=f"{system}\n\n{user_text}",
    )

    if cache_key:
        cached = get_cached(cache_key, prompt.version)
        if cached is not None:
            hit = _from_cache(cached, schema)
            if hit is not None:
                tracker.cache_hit = True
                tracker.success = True
                tracker.finish()
                return hit

    check_quota(user, task)  # raises QuotaExceeded before any provider call

    messages = [{"role": "user", "content": user_text}]
    provider = get_provider()
    try:
        result = _call_and_validate(
            provider, tracker, model, system, messages, max_output_tokens, schema, task
        )
    except LLMError as exc:
        tracker.error = (
            tracker.error or f"{type(exc).__name__}: {exc.args[0] if exc.args else ''}"[:300]
        )
        tracker.finish()
        raise
    except Exception as exc:
        tracker.error = f"unexpected: {type(exc).__name__}"
        tracker.finish()
        raise

    tracker.success = True
    tracker.finish()
    if cache_key:
        store_cached(cache_key, prompt.version, _to_cache(result))
    return result


def _call_and_validate(
    provider: Any,
    tracker: _Tracker,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    schema: type[BaseModel] | None,
    task: str,
) -> str | BaseModel:
    response = provider.complete(
        model=model, system=system, messages=messages, max_tokens=max_tokens,
        json_mode=schema is not None, task=task, schema=schema,
    )  # fmt: skip
    tracker.add(response)
    tracker.response_body = response.text

    if schema is None:
        text = response.text.strip()
        if not text:
            tracker.error = "empty_response"
            raise LLMOutputError("empty_response")
        return text

    try:
        return schema.model_validate(extract_json_object(response.text))
    except (ValueError, ValidationError) as first_error:
        log_event(
            logger,
            logging.WARNING,
            "llm_repair_retry",
            "LLM output failed validation; retrying once",
            task=task,
            model=model,
        )
        repair = [
            *messages,
            {"role": "assistant", "content": response.text},
            {
                "role": "user",
                "content": (
                    f"Your previous reply was not valid: {_validation_summary(first_error)}. "
                    "Reply again with only a JSON object that matches the schema."
                ),
            },
        ]
        retry = provider.complete(
            model=model, system=system, messages=repair, max_tokens=max_tokens,
            json_mode=True, task=task, schema=schema,
        )  # fmt: skip
        tracker.add(retry)
        tracker.response_body = retry.text
        try:
            return schema.model_validate(extract_json_object(retry.text))
        except (ValueError, ValidationError) as second_error:
            tracker.error = _failed_fields(second_error)
            raise LLMOutputError(tracker.error) from second_error


def _to_cache(result: str | BaseModel) -> Any:
    return {"text": result} if isinstance(result, str) else result.model_dump(mode="json")


def _from_cache(cached: Any, schema: type[BaseModel] | None) -> str | BaseModel | None:
    """A cached value that no longer fits the schema is ignored, not trusted."""
    try:
        if schema is None:
            text = cached["text"]
            return text if isinstance(text, str) and text else None
        return schema.model_validate(cached)
    except (KeyError, TypeError, ValidationError):
        return None


def call_structured[T: BaseModel](
    *,
    task: str,
    prompt_name: str,
    variables: dict[str, Any],
    schema: type[T],
    user: User | None,
    model_tier: Tier | None = None,
    max_output_tokens: int = 1500,
    cache_key: str | None = None,
) -> T:
    """Run a prompt and return the output validated against `schema`.

    `user=None` is system-paid work (counted against the daily system cap, not a user quota).
    `cache_key`, when given, must already include anything that makes the result user-specific.
    """
    result = _run(
        task=task, prompt_name=prompt_name, variables=variables, schema=schema, user=user,
        model_tier=model_tier, max_output_tokens=max_output_tokens, cache_key=cache_key,
    )  # fmt: skip
    if not isinstance(result, schema):  # cannot happen: a schema always yields a model
        raise TypeError("structured call returned text")
    return result


def call_text(
    *,
    task: str,
    prompt_name: str,
    variables: dict[str, Any],
    user: User | None,
    model_tier: Tier | None = None,
    max_output_tokens: int = 1500,
    cache_key: str | None = None,
) -> str:
    """Like `call_structured`, for free-text output such as a cover letter."""
    result = _run(
        task=task, prompt_name=prompt_name, variables=variables, schema=None, user=user,
        model_tier=model_tier, max_output_tokens=max_output_tokens, cache_key=cache_key,
    )  # fmt: skip
    if not isinstance(result, str):  # cannot happen: no schema always yields text
        raise TypeError("text call returned a model")
    return result
