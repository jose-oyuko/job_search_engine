"""Huey tasks for jobs (spec §18). Thin wrappers over `services`."""

import logging

from huey import crontab
from huey.contrib.djhuey import db_task, lock_task, periodic_task

from apps.core.logging import log_event
from apps.core.tasks import logged_task
from apps.sources.models import Source
from apps.sources.services import recheck_stale_jobs

from . import services

logger = logging.getLogger(__name__)


@db_task()
@logged_task
def extract_job_task(job_id: int) -> None:
    """LLM extraction for one new job (system-paid, once per job). Idempotent."""
    services.extract_job_details(job_id)


@periodic_task(crontab(hour="2", minute="0"), name="jobs.expire_stale")
@logged_task
def expire_stale() -> None:
    """Daily 02:00 UTC: re-check jobs that dropped off their listing, then expire stale ones."""
    with lock_task("jobs.expire_stale"):
        rechecked = sum(
            recheck_stale_jobs(source) for source in Source.objects.filter(enabled=True)
        )
        by_age = services.expire_by_age()
        log_event(
            logger, logging.INFO, "expire_stale_finished", "Expiry run finished",
            expired_by_recheck=rechecked, expired_by_age=by_age,
        )  # fmt: skip
