"""Running sources: collect, ingest, record the run and track source health (spec §6.6).

`run_source` is what the scheduler, the admin "Run now" action and `test_source` all call.
Health: one failure makes a source `degraded`; three in a row make it `failing`; a success resets
it. "No job IDs found" counts as a failure, which is how a changed site (no matching links) shows up
as a degraded or failing source instead of a silent crash.
"""

import datetime
import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from django.db import transaction
from django.utils import timezone

from apps.core.logging import log_event
from apps.jobs.models import Job
from apps.jobs.services import IngestResult, ingest_raw_jobs, touch_seen

from . import http
from .base import RawJob
from .exceptions import RateLimited, SourceError
from .models import Source, SourceRun
from .registry import fetcher_class

logger = logging.getLogger(__name__)

FAILING_AFTER = 3
OVERLAP_MINUTES = 30
MAX_RECORDED_ERRORS = 20
RECHECK_AFTER_DAYS = 2
RECHECK_LIMIT = 20

SEEN = "seen"


@dataclass
class Collected:
    """What a fetcher produced in one run, before anything is saved."""

    raw_jobs: list[RawJob] = field(default_factory=list)
    ids_seen: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fatal: str = ""  # set when the whole run failed (list page unreachable, rate limited, ...)
    returns_everything: bool = False
    attempted: int = 0  # how many IDs we tried to fetch details for


def health_for(consecutive_failures: int) -> str:
    if consecutive_failures >= FAILING_AFTER:
        return Source.Health.FAILING
    return Source.Health.DEGRADED if consecutive_failures else Source.Health.OK


def collect(
    source: Source,
    *,
    client: httpx.Client,
    seen_ids: set[str],
    sleep: Callable[[float], None] = time.sleep,
    limit: int | None = None,
) -> Collected:
    """Run the source's fetcher and gather what it returns. Partial results are kept when a
    detail fetch fails; a failure of the listing itself is recorded as `fatal`."""
    out = Collected()
    fetcher = fetcher_class(source)(source, client, sleep=sleep, limit=limit)
    out.returns_everything = fetcher.returns_everything
    try:
        for raw in fetcher.fetch_all_new(seen_ids):
            out.raw_jobs.append(raw)
    except RateLimited as exc:
        out.fatal = "rate_limited" + (
            f":retry_after={exc.retry_after:g}s" if exc.retry_after else ""
        )
    except SourceError as exc:
        out.fatal = str(exc)
    out.ids_seen = list(dict.fromkeys(fetcher.ids_seen))
    out.errors = list(fetcher.errors)
    out.attempted = (
        len(out.ids_seen) if fetcher.returns_everything else len(set(out.ids_seen) - seen_ids)
    )
    return out


def _outcome(source: Source, out: Collected) -> tuple[bool, list[str]]:
    """Decide success and the list of error strings for the run record."""
    errors = ([out.fatal] if out.fatal else []) + out.errors
    if out.fatal:
        return False, errors
    if not out.ids_seen and not source.config.get("allow_empty"):
        return False, [*errors, "no_ids_found"]
    if out.attempted and not out.raw_jobs and out.errors:
        return False, errors  # every detail fetch failed
    return True, errors


def run_source(
    source: Source,
    *,
    trigger: str = SourceRun.Trigger.SCHEDULE,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    limit: int | None = None,
) -> SourceRun | None:
    """Fetch one source, ingest what it returns and update its health. Returns the run record, or
    None if another run of this source is still in progress."""
    now = timezone.now()
    overlap_cutoff = now - datetime.timedelta(minutes=OVERLAP_MINUTES)
    if SourceRun.objects.filter(
        source=source, finished_at__isnull=True, started_at__gte=overlap_cutoff
    ).exists():
        log_event(
            logger,
            logging.INFO,
            "source_run_skipped",
            "Run already in progress",
            source=source.name,
        )
        return None

    run = SourceRun.objects.create(source=source, started_at=now, trigger=trigger)
    Source.objects.filter(pk=source.pk).update(last_run_at=now)
    log_event(
        logger, logging.INFO, "source_run_started", "Source run started",
        source=source.name, run_id=run.pk, trigger=trigger,
    )  # fmt: skip

    owns_client = client is None
    client = client or http.build_client()
    started = time.monotonic()
    try:
        seen_ids = set(Job.objects.filter(source=source).values_list("external_id", flat=True))
        out = collect(source, client=client, seen_ids=seen_ids, sleep=sleep, limit=limit)
        success, errors = _outcome(source, out)
        result = IngestResult()
        if out.raw_jobs:
            result = ingest_raw_jobs(source, out.raw_jobs)
        if not out.returns_everything:
            fetched = {raw.external_id for raw in out.raw_jobs}
            touch_seen(source, set(out.ids_seen) - fetched)
        errors += result.errors
    except Exception as exc:  # noqa: BLE001 - a bug in one fetcher must not kill the scheduler
        log_event(
            logger, logging.ERROR, "source_run_crashed", "Source run crashed",
            source=source.name, run_id=run.pk, exc_info=True,
        )  # fmt: skip
        out, result = Collected(), IngestResult()
        success, errors = False, [f"crashed:{type(exc).__name__}"]
    finally:
        if owns_client:
            client.close()

    _finish(
        source,
        run,
        out,
        result,
        success,
        errors,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return run


def _finish(
    source: Source,
    run: SourceRun,
    out: Collected,
    result: IngestResult,
    success: bool,
    errors: list[str],
    *,
    duration_ms: int,
) -> None:
    finished = timezone.now()
    run.finished_at = finished
    run.ids_seen = len(out.ids_seen)
    run.new_jobs = result.new
    run.updated_jobs = result.updated
    run.errors = errors[:MAX_RECORDED_ERRORS]
    run.success = success
    run.save()

    source.refresh_from_db()
    previous_health = source.health
    if success:
        source.consecutive_failures = 0
        source.last_success_at = finished
    else:
        source.consecutive_failures += 1
    source.health = health_for(source.consecutive_failures)
    source.save(update_fields=["consecutive_failures", "last_success_at", "health", "updated_at"])

    log_event(
        logger, logging.INFO if success else logging.WARNING, "source_run_finished",
        "Source run finished",
        source=source.name, run_id=run.pk, ids_seen=run.ids_seen, new=run.new_jobs,
        updated=run.updated_jobs, errors=len(errors), success=success, duration_ms=duration_ms,
    )  # fmt: skip
    if source.health != previous_health:
        log_event(
            logger,
            logging.ERROR if source.health == Source.Health.FAILING else logging.WARNING,
            "source_health_changed", "Source health changed",
            source=source.name, old=previous_health, new=source.health,
            consecutive_failures=source.consecutive_failures,
        )  # fmt: skip
        # TODO(phase 8): send a `source_health` notification to staff users on Telegram.
    _enqueue_extraction(result)


def _enqueue_extraction(result: IngestResult) -> None:
    ids = [*result.new_job_ids, *result.changed_job_ids]
    if not ids:
        return
    from apps.core.tasks import enqueue
    from apps.jobs.tasks import extract_job_task

    for job_id in ids:
        # After commit, so the worker can see the row. TODO(phase 6): then run match_new_job.
        transaction.on_commit(functools.partial(enqueue, extract_job_task, job_id))


def due_sources(now: datetime.datetime | None = None) -> list[Source]:
    """Enabled sources whose interval has passed since their last run."""
    now = now or timezone.now()
    return [
        source
        for source in Source.objects.filter(enabled=True)
        if source.last_run_at is None
        or source.last_run_at + datetime.timedelta(minutes=source.interval_minutes) <= now
    ]


# --- re-checking stale jobs (used by the daily expiry task) ---------------------------------


def recheck_stale_jobs(
    source: Source,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime.datetime | None = None,
) -> int:
    """For jobs not seen in a while, ask the source whether the page is gone (404/410) and expire
    those that are. Politeness: at most `RECHECK_LIMIT` requests per source per run. Returns the
    number expired."""
    now = now or timezone.now()
    stale = list(
        Job.objects.active()
        .filter(source=source, last_seen_at__lt=now - datetime.timedelta(days=RECHECK_AFTER_DAYS))
        .order_by("last_seen_at")[:RECHECK_LIMIT]
    )
    if not stale:
        return 0
    owns_client = client is None
    client = client or http.build_client()
    expired = 0
    try:
        fetcher = fetcher_class(source)(source, client, sleep=sleep)
        for position, job in enumerate(stale):
            if position:
                fetcher.pause()
            if fetcher.is_gone(job.external_id, job.url):
                Job.objects.filter(pk=job.pk).update(status=Job.Status.EXPIRED)
                expired += 1
    finally:
        if owns_client:
            client.close()
    log_event(
        logger, logging.INFO, "job_recheck_finished", "Stale jobs re-checked",
        source=source.name, checked=len(stale), expired=expired,
    )  # fmt: skip
    return expired


# --- seeding --------------------------------------------------------------------------------


EXPERTWOKA = {
    "name": "expertwoka",
    "display_name": "ExpertWoka",
    "kind": Source.Kind.CUSTOM,
    "interval_minutes": 20,
    "config": {
        "list_url": "https://expertwoka.com/opportunities?sort=newest&page=1",
        "max_new_per_run": 30,
        "expire_after_days": 7,
    },
    "notes": (
        "Aggregator of AI-training and remote roles. Unfiltered newest list; eligibility is "
        "filtered by us. The site's own location filter is ?location=KE (also region codes, "
        "GLOBAL)."
    ),
}


def seed_sources() -> list[tuple[str, bool]]:
    """Create the built-in sources if missing. Existing rows are left alone so admin edits stick.
    Returns `(name, created)` pairs."""
    created: list[tuple[str, bool]] = []
    for spec in (EXPERTWOKA,):
        _, was_created = Source.objects.get_or_create(name=spec["name"], defaults=spec)
        created.append((str(spec["name"]), was_created))
    return created
