"""Deterministic provider for tests and for running the app without an API key (spec §11.2).

Responses come, in order, from: scripted items (tests), `<LLM_FAKE_FIXTURES_DIR>/<task>.json`
(or `.txt` for text tasks), then a schema-valid default built from the output schema.
"""

import json
import types
import typing
from collections import deque
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from django.conf import settings
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from .base import ProviderResponse


class FakeProvider:
    name = "fake"

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self.fixtures_dir = fixtures_dir
        # Tests push strings (returned as the next response) or exceptions (raised) here.
        self.script: deque[str | Exception] = deque()
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {"model": model, "system": system, "messages": messages, "task": task, "schema": schema}
        )
        text = self._next_text(task, schema)
        prompt_chars = len(system) + sum(len(m["content"]) for m in messages)
        return ProviderResponse(
            text=text,
            input_tokens=max(1, prompt_chars // 4),
            output_tokens=max(1, len(text) // 4),
        )

    def _next_text(self, task: str, schema: type[BaseModel] | None) -> str:
        if self.script:
            item = self.script.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        fixtures = self.fixtures_dir or Path(settings.LLM_FAKE_FIXTURES_DIR)
        if task:
            json_file, txt_file = fixtures / f"{task}.json", fixtures / f"{task}.txt"
            if json_file.is_file():
                return json_file.read_text(encoding="utf-8")
            if txt_file.is_file():
                return txt_file.read_text(encoding="utf-8").strip()
        if schema is None:
            return "This is a placeholder response from the fake LLM provider."
        return json.dumps(default_for(schema))


def default_for(annotation: Any) -> Any:
    """A schema-valid, deterministic value for a type annotation (used when no fixture exists)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        values: dict[str, Any] = {}
        for name, field in annotation.model_fields.items():
            if field.default is not PydanticUndefined:
                values[name] = field.default
            elif field.default_factory is not None:
                values[name] = field.default_factory()  # type: ignore[call-arg]
            else:
                values[name] = default_for(field.annotation)
        return values
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return None if len(args) < len(get_args(annotation)) else default_for(args[0])
    if origin in (list, set, frozenset, typing.Sequence):
        return []
    if origin is dict:
        return {}
    if annotation is bool:
        return True
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    return "example"
