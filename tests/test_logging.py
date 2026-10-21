import json
import logging

import pytest
from django.test import Client

from apps.core.logging import ConsoleFormatter, log_event, request_id_var
from tests.logcapture import ListHandler


def test_json_formatter_emits_valid_json_with_standard_fields(handler: ListHandler) -> None:
    log_event(logging.getLogger("t"), logging.INFO, "source_run_finished", "done", count=3)
    (record,) = handler.events("source_run_finished")
    for field in ("ts", "level", "logger", "event", "msg", "env", "release", "service"):
        assert field in record
    assert record["level"] == "INFO"
    assert record["count"] == 3
    assert record["env"] == "test" and record["release"] == "abc123"
    assert str(record["ts"]).endswith("+00:00")


def test_redaction_masks_password_token_and_email(handler: ListHandler) -> None:
    log_event(
        logging.getLogger("t"),
        logging.INFO,
        "login_attempt",
        "login by jane.doe@example.com",
        password="hunter2",
        reset_token="abc-123",
        api_key="sk-live",
        nested={"Authorization": "Bearer xyz", "ok": "fine"},
        note="contact bob@example.org please",
    )
    (record,) = handler.events("login_attempt")
    raw = json.dumps(record)
    assert record["password"] == "[redacted]"
    assert record["reset_token"] == "[redacted]"
    assert record["api_key"] == "[redacted]"
    assert record["nested"] == {"Authorization": "[redacted]", "ok": "fine"}
    for leaked in ("hunter2", "abc-123", "sk-live", "Bearer xyz", "jane.doe@", "bob@example"):
        assert leaked not in raw


def test_exception_text_is_emitted_and_masked(handler: ListHandler) -> None:
    try:
        raise ValueError("failed for carol@example.com")
    except ValueError:
        log_event(logging.getLogger("t"), logging.ERROR, "boom", "x", exc_info=True)
    (record,) = handler.events("boom")
    assert "ValueError" in str(record["exc_info"])
    assert "carol@example.com" not in str(record["exc_info"])


def test_console_formatter_is_single_line_with_event() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)
    record.event = "greeting"
    record.count = 2
    line = ConsoleFormatter().format(record)
    assert "greeting: hello" in line and "count=2" in line and "\n" not in line


@pytest.mark.django_db
def test_request_id_propagates_from_middleware_into_logs_and_response(
    handler: ListHandler,
) -> None:
    response = Client().get("/", headers={"X-Request-ID": "req-12345678"})
    assert response["X-Request-ID"] == "req-12345678"
    (access,) = handler.events("http_request")
    assert access["request_id"] == "req-12345678"
    assert access["status"] == 200
    assert "duration_ms" in access


@pytest.mark.django_db
def test_request_id_is_generated_when_missing_or_invalid(handler: ListHandler) -> None:
    for header in ({}, {"X-Request-ID": "bad id with spaces"}):
        response = Client().get("/", headers=header)
        assert len(response["X-Request-ID"]) == 32
    assert request_id_var.get() is None  # context is cleaned up after the request


@pytest.mark.django_db
def test_healthz_is_not_access_logged(handler: ListHandler) -> None:
    Client().get("/healthz")
    assert not handler.events("http_request")
