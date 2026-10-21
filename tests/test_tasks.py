import logging
from datetime import datetime

import pytest
from django.conf import settings
from huey.contrib.djhuey import db_task

from apps.core.logging import request_id_var, task_id_var
from apps.core.tasks import enqueue, heartbeat, logged_task
from tests.logcapture import ListHandler


def test_logged_task_logs_start_and_finish_with_ids(handler: ListHandler) -> None:
    @logged_task
    def work(x: int) -> int:
        assert task_id_var.get() is not None
        return x * 2

    assert work(2) == 4
    (started,) = handler.events("task_started")
    (finished,) = handler.events("task_finished")
    assert started["task_id"] == finished["task_id"]
    assert "duration_ms" in finished
    assert task_id_var.get() is None


def test_logged_task_logs_and_reraises_failures(handler: ListHandler) -> None:
    @logged_task
    def broken() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        broken()
    (failed,) = handler.events("task_failed")
    assert failed["level"] == "ERROR"
    assert "RuntimeError" in str(failed["exc_info"])
    assert not handler.events("task_finished")


def test_request_id_flows_into_enqueued_task(handler: ListHandler) -> None:
    @db_task()
    @logged_task
    def child() -> None:
        logging.getLogger("t").info("inside")

    token = request_id_var.set("req-abcdef12")
    try:
        enqueue(child)
    finally:
        request_id_var.reset(token)

    assert handler.events("task_started")[0]["request_id"] == "req-abcdef12"


def test_heartbeat_logs_task_id_and_duration(handler: ListHandler) -> None:
    heartbeat.call_local()
    (beat,) = handler.events("heartbeat")
    (finished,) = handler.events("task_finished")
    assert beat["task_id"] == finished["task_id"] is not None
    assert isinstance(finished["duration_ms"], int)


def test_heartbeat_is_scheduled_every_ten_minutes() -> None:
    assert settings.HEARTBEAT_INTERVAL_MINUTES == 10
    task = heartbeat.task_class()
    assert task.validate_datetime(datetime(2026, 1, 1, 12, 10))
    assert task.validate_datetime(datetime(2026, 1, 1, 12, 20))
    assert not task.validate_datetime(datetime(2026, 1, 1, 12, 15))
