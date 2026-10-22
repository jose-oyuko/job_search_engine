"""OpenAI-compatible Chat Completions over httpx (spec §11.2).

Works with OpenAI, OpenRouter, Groq and similar. The API key and request/response bodies are
never logged; errors carry only a status code and the provider's short error type.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel

from apps.core.logging import log_event
from apps.llm.exceptions import LLMProviderError

from .base import ProviderResponse

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
MAX_RETRY_AFTER_SECONDS = 10.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 60,
        json_mode_supported: bool = True,
        max_tokens_param: str = "max_tokens",
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self.json_mode_supported = json_mode_supported
        self.max_tokens_param = max_tokens_param
        self._client = client or httpx.Client(timeout=timeout)
        self._sleep = sleep

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        json_mode: bool,
        task: str = "",
        schema: type[BaseModel] | None = None,
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            self.max_tokens_param: max_tokens,
        }
        if json_mode and self.json_mode_supported:
            payload["response_format"] = {"type": "json_object"}
        response = self._post_with_retries(payload)
        return self._parse(response)

    def _post_with_retries(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        for attempt in range(MAX_RETRIES + 1):
            last_attempt = attempt == MAX_RETRIES
            try:
                response = self._client.post(self.url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if last_attempt:
                    raise LLMProviderError(f"transport_error:{type(exc).__name__}") from exc
                self._backoff(attempt, None, reason=type(exc).__name__)
                continue
            if response.status_code < 400:
                return response
            if response.status_code in RETRYABLE_STATUSES and not last_attempt:
                self._backoff(attempt, response, reason=str(response.status_code))
                continue
            raise LLMProviderError(f"http_{response.status_code}:{_error_type(response)}")
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, response: httpx.Response | None, *, reason: str) -> None:
        delay = float(2**attempt)  # 1s, then 2s
        if response is not None and response.status_code == 429:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.replace(".", "", 1).isdigit():
                delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        log_event(
            logger,
            logging.WARNING,
            "llm_provider_retry",
            "Retrying the LLM provider",
            reason=reason,
            attempt=attempt + 1,
            delay_s=delay,
        )
        self._sleep(delay)

    @staticmethod
    def _parse(response: httpx.Response) -> ProviderResponse:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("malformed_response") from exc
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            raise LLMProviderError("malformed_response")
        usage = body.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        return ProviderResponse(
            text=content,
            input_tokens=int(input_tokens) if input_tokens is not None else 0,
            output_tokens=int(output_tokens)
            if output_tokens is not None
            else max(1, len(content) // 4),
        )


def _error_type(response: httpx.Response) -> str:
    """The provider's short error type or code, never its message (which may echo input)."""
    try:
        error = response.json().get("error")
    except ValueError:
        return "unknown"
    if isinstance(error, dict):
        return str(error.get("type") or error.get("code") or "unknown")[:60]
    return "unknown"
