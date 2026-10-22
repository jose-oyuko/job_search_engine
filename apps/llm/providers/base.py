"""The provider interface (spec §11.2)."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int


class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        json_mode: bool,
        # Hints for the fake provider; real providers ignore them.
        task: str = "",
        schema: type[BaseModel] | None = None,
    ) -> ProviderResponse: ...
