"""Capture log output as parsed JSON, running the same filters as production."""

import json
import logging

from apps.core.logging import ContextFilter, JsonFormatter, RedactionFilter


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.addFilter(RedactionFilter())
        self.addFilter(ContextFilter(environment="test", release="abc123", service="web"))
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.lines]

    def events(self, event: str) -> list[dict[str, object]]:
        return [r for r in self.records() if r["event"] == event]
