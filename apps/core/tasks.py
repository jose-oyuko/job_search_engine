"""Task infrastructure (`@logged_task`) and the example periodic task."""

import functools
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import sentry_sdk
from django.conf import settings
from huey import crontab
from huey.contrib.djhuey import lock_task, periodic_task
from huey.exceptions import TaskLockedException

from apps.core.logging import log_event, request_id_var, task_id_var

logger = logging.getLogger(__name__)


def logged_task[**P, R](func: Callable[P, R]) -> Callable[P, R | None]:
    """Wrap a Huey task body with correlation IDs, start/finish/failure logs and Sentry.

    Apply *under* the Huey decorator: `@db_task()` / `@periodic_task(...)` then `@logged_task`.
    Enqueue callers may pass `request_id=` so the originating request can be traced.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        origin_request_id: str | None = kwargs.pop("request_id", None)  # type: ignore[assignment]
        task_id = uuid.uuid4().hex
        task_token = task_id_var.set(task_id)
        request_token = request_id_var.set(origin_request_id) if origin_request_id else None
        name = f"{func.__module__}.{func.__name__}"
        sentry_sdk.set_tag("task_id", task_id)
        started = time.monotonic()
        log_event(logger, logging.INFO, "task_started", "Task started", task=name)
        try:
            result = func(*args, **kwargs)
        except TaskLockedException:
            log_event(logger, logging.INFO, "task_skipped", "Task already running", task=name)
            return None
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "task_failed",
                "Task failed",
                task=name,
                duration_ms=round((time.monotonic() - started) * 1000),
                exc_info=True,
            )
            sentry_sdk.capture_exception(exc)
            raise
        else:
            log_event(
                logger,
                logging.INFO,
                "task_finished",
                "Task finished",
                task=name,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return result
        finally:
            task_id_var.reset(task_token)
            if request_token is not None:
                request_id_var.reset(request_token)

    return wrapper


def enqueue(task: Any, *args: Any, **kwargs: Any) -> Any:
    """Enqueue a `@logged_task` Huey task, carrying the current request ID with it."""
    request_id = request_id_var.get()
    if request_id:
        kwargs["request_id"] = request_id
    return task(*args, **kwargs)


@periodic_task(
    crontab(minute=f"*/{settings.HEARTBEAT_INTERVAL_MINUTES}"),
    name="core.heartbeat",
)
@logged_task
def heartbeat() -> None:
    """Proves the worker and scheduler are alive. Phase 12 adds the Healthchecks ping."""
    with lock_task("core.heartbeat"):
        log_event(logger, logging.INFO, "heartbeat", "Worker heartbeat")
