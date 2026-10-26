"""Huey tasks for notifications (spec §18). Thin wrappers over `services`."""

import logging

from huey import crontab
from huey.contrib.djhuey import db_periodic_task, db_task, lock_task

from apps.core.logging import log_event
from apps.core.tasks import logged_task

from . import services

logger = logging.getLogger(__name__)


@db_task()
@logged_task
def deliver_notification_task(notification_id: int) -> None:
    """Send one notification. Idempotent: a sent, skipped or claimed row is left alone."""
    services.deliver(notification_id)


@db_periodic_task(crontab(minute="*/5"), name="notifications.dispatch")
@logged_task
def dispatch() -> None:
    """Every 5 minutes: send due digests and deferred alerts."""
    with lock_task("notifications.dispatch"):
        counts = services.dispatch()
        log_event(
            logger, logging.INFO, "dispatch_finished", "Notification dispatch finished", **counts
        )
