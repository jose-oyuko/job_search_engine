"""Domain exceptions for the LLM layer. Raised by the client and services, handled at boundaries."""


class LLMError(Exception):
    """Base class. `message` is safe to show a user; the rest of the detail goes to logs."""

    message = "The AI service could not complete this request. Please try again later."

    def __str__(self) -> str:
        return self.message


class QuotaExceeded(LLMError):
    """A per-user monthly limit or the system daily cap was reached before any provider call."""

    def __init__(self, scope: str, message: str | None = None) -> None:
        super().__init__(scope)
        self.scope = scope
        self.message = message or "You have reached your monthly AI usage limit."


class LLMOutputError(LLMError):
    """The provider answered, but not with output that fits the schema (after one repair retry)."""

    message = "The AI returned an unusable answer. Please try again."


class LLMProviderError(LLMError):
    """The provider could not be reached, or refused the request."""


class LLMConfigError(LLMError):
    """The LLM settings are incomplete or invalid (an operator problem, not a user problem)."""

    message = "The AI service is not configured."


class PromptError(LLMError):
    """A prompt file is missing or malformed, or a variable it needs was not supplied."""

    message = "The AI service is misconfigured."
