"""Huey tasks for the tracker (spec §18). Thin wrappers over `services`."""

import logging

from huey import crontab
from huey.contrib.djhuey import db_periodic_task, lock_task

from apps.core.tasks import logged_task

from . import services

logger = logging.getLogger(__name__)


@db_periodic_task(crontab(hour="7", minute="0"), name="tracker.followup_reminders")
@logged_task
def followup_reminders() -> None:
    """Daily at 07:00 UTC: queue follow-up reminders and ghosted suggestions."""
    with lock_task("tracker.followup_reminders"):
        services.send_followup_reminders()
