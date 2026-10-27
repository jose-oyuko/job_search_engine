"""Huey tasks for email ingestion (spec §18). Thin wrappers over `services`."""

import logging

from huey import crontab
from huey.contrib.djhuey import db_periodic_task, lock_task

from apps.core.logging import log_event
from apps.core.tasks import logged_task

from . import services

logger = logging.getLogger(__name__)


@db_periodic_task(crontab(minute="*/5"), name="inbound.poll_mailbox")
@logged_task
def poll_mailbox() -> None:
    """Every 5 minutes: read the dedicated inbox and ingest what is in it."""
    with lock_task("inbound.poll_mailbox"):
        counts = services.poll_mailbox()
        if counts["messages"]:
            log_event(logger, logging.INFO, "inbound_poll_finished", "Mailbox polled", **counts)


@db_periodic_task(crontab(hour="3", minute="30"), name="inbound.clear_old_bodies")
@logged_task
def clear_old_bodies() -> None:
    """Daily 03:30 UTC: delete email text older than 30 days (spec §9.3)."""
    with lock_task("inbound.clear_old_bodies"):
        cleared = services.clear_old_bodies()
        log_event(
            logger, logging.INFO, "inbound_retention", "Old email bodies cleared", cleared=cleared
        )
