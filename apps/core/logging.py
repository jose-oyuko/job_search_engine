"""Structured logging (spec §21.1).

Importable from settings, so it must not import Django models or settings at module level.
"""

import contextvars
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_id", default=None)
user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("user_id", default=None)

SENSITIVE_KEY_RE = re.compile(
    r"password|token|secret|api_key|authorization|cookie|session", re.IGNORECASE
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MASK = "[redacted]"

# Attributes every LogRecord has, plus the ones our filters inject. Anything else on a record
# came from `extra=` and is emitted as an event-specific field.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "event",
    "env",
    "release",
    "service",
}
_CONTEXT_ATTRS = ("request_id", "task_id", "user_id")


def mask_text(text: str) -> str:
    """Mask email addresses in free text (safety net, not a substitute for careful logging)."""
    return EMAIL_RE.sub(MASK, text)


def _redact_value(key: str, value: Any) -> Any:
    if SENSITIVE_KEY_RE.search(key):
        return MASK
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, str):
        return mask_text(value)
    return value


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        k: v
        for k, v in record.__dict__.items()
        if k not in _STANDARD_ATTRS and k not in _CONTEXT_ATTRS and not k.startswith("_")
    }


def log_event(logger: logging.Logger, level: int, event: str, msg: str, **fields: Any) -> None:
    """Log `msg` with a snake_case `event` name and structured fields."""
    exc_info = fields.pop("exc_info", None)
    logger.log(level, msg, extra={"event": event, **fields}, exc_info=exc_info)


class ContextFilter(logging.Filter):
    """Stamps every record with static deployment info and the current request/task/user."""

    def __init__(self, environment: str = "", release: str = "", service: str = "web") -> None:
        super().__init__()
        self.environment = environment
        self.release = release
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.env = self.environment
        record.release = self.release
        record.service = self.service
        record.request_id = request_id_var.get()
        record.task_id = task_id_var.get()
        record.user_id = user_id_var.get()
        if not hasattr(record, "event"):
            record.event = "log"
        return True


class RedactionFilter(logging.Filter):
    """Masks sensitive `extra` values and email addresses before any handler formats them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_text(record.getMessage())
        record.args = None
        for key, value in _extra_fields(record).items():
            setattr(record, key, _redact_value(key, value))
        return True


def _utc_iso(created: float) -> str:
    return datetime.fromtimestamp(created, UTC).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
            "env": getattr(record, "env", ""),
            "release": getattr(record, "release", ""),
            "service": getattr(record, "service", ""),
            "request_id": getattr(record, "request_id", None),
            "task_id": getattr(record, "task_id", None),
            "user_id": getattr(record, "user_id", None),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc_info"] = mask_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "request_id", None) or getattr(record, "task_id", None) or "-"
        fields = " ".join(f"{k}={v}" for k, v in _extra_fields(record).items())
        line = (
            f"{_utc_iso(record.created)} {record.levelname:<8} {record.name} [{ctx}] "
            f"{getattr(record, 'event', 'log')}: {record.getMessage()}"
        )
        if fields:
            line += f" | {fields}"
        if record.exc_info:
            line += "\n" + mask_text(self.formatException(record.exc_info))
        return line


def build_logging_config(
    *, level: str, fmt: str, environment: str, release: str, service: str
) -> dict[str, Any]:
    formatter = "json" if fmt == "json" else "console"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "context": {
                "()": "apps.core.logging.ContextFilter",
                "environment": environment,
                "release": release,
                "service": service,
            },
            "redaction": {"()": "apps.core.logging.RedactionFilter"},
        },
        "formatters": {
            "json": {"()": "apps.core.logging.JsonFormatter"},
            "console": {"()": "apps.core.logging.ConsoleFormatter"},
        },
        "handlers": {
            # stdout only (twelve-factor); Docker's json-file driver handles rotation.
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": formatter,
                # Order matters: redact first, then add context.
                "filters": ["redaction", "context"],
            },
        },
        "root": {"handlers": ["stdout"], "level": level},
        "loggers": {
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "urllib3": {"level": "WARNING"},
            "PIL": {"level": "WARNING"},
            "weasyprint": {"level": "WARNING"},
            "django.request": {"level": "WARNING"},
            "django.security": {"level": "WARNING"},
            "django.db.backends": {"level": "INFO"},
        },
    }
