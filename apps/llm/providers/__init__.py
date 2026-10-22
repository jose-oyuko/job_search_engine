"""Provider selection from settings."""

from functools import lru_cache

from django.conf import settings

from apps.llm.exceptions import LLMConfigError

from .base import LLMProvider, ProviderResponse

__all__ = ["LLMProvider", "ProviderResponse", "get_provider"]


@lru_cache(maxsize=1)
def _build(
    provider: str, base_url: str, api_key: str, timeout: int, json_mode: bool, param: str
) -> LLMProvider:
    if provider == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    if provider == "openai_compat":
        from .openai_compat import OpenAICompatProvider

        if not api_key:
            raise LLMConfigError("LLM_API_KEY is not set")
        return OpenAICompatProvider(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            json_mode_supported=json_mode,
            max_tokens_param=param,
        )
    raise LLMConfigError(f"unknown LLM_PROVIDER: {provider!r}")


def get_provider() -> LLMProvider:
    """The configured provider. Cached while settings are unchanged (tests override settings)."""
    return _build(
        settings.LLM_PROVIDER,
        settings.LLM_BASE_URL,
        settings.LLM_API_KEY,
        settings.LLM_TIMEOUT_SECONDS,
        settings.LLM_JSON_MODE,
        settings.LLM_MAX_TOKENS_PARAM,
    )
