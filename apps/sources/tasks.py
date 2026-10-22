"""Huey tasks for sources (spec §18). Thin wrappers over `services`."""

import logging

from huey import crontab
from huey.contrib.djhuey import db_task, lock_task, periodic_task

from apps.core.logging import log_event
from apps.core.tasks import enqueue, logged_task

from . import services
from .models import Source, SourceRun

logger = logging.getLogger(__name__)


@db_task()
@logged_task
def run_source_task(source_id: int, trigger: str = SourceRun.Trigger.SCHEDULE) -> None:
    """Fetch one source. A per-source lock keeps two workers from running it at once."""
    source = Source.objects.filter(pk=source_id).first()
    if source is None:
        return
    with lock_task(f"run_source:{source_id}"):
        services.run_source(source, trigger=trigger)


@periodic_task(crontab(minute="*/5"), name="sources.tick")
@logged_task
def tick() -> None:
    """Every 5 minutes: queue a run for each enabled source whose interval has passed."""
    with lock_task("sources.tick"):
        due = services.due_sources()
        for source in due:
            enqueue(run_source_task, source.pk, SourceRun.Trigger.SCHEDULE)
        log_event(logger, logging.INFO, "sources_tick", "Scheduler tick", due=len(due))
