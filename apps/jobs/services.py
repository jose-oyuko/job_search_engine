"""Job ingestion: upsert, normalisation, deduplication, expiry and LLM extraction (spec §7).

Fetchers return `RawJob`s; this module turns them into `Job` rows. It never logs titles,
companies or descriptions, only counts and IDs.
"""

import datetime
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.core.logging import log_event
from apps.core.sanitize import clean_html, html_to_text
from apps.llm.client import call_structured
from apps.llm.exceptions import LLMError, QuotaExceeded
from apps.llm.models import LLMCall
from apps.llm.schemas import JobExtraction
from apps.llm.untrusted import MAX_JOB_CHARS, wrap_untrusted
from apps.sources.base import RawJob
from apps.sources.models import Source

from .eligibility import parse_locations
from .models import Job
from .pay import UNKNOWN, parse_pay
from .scam import detect_scam_flags

logger = logging.getLogger(__name__)

DEDUPE_WINDOW_DAYS = 30
MANUAL_JOB_EXPIRY_DAYS = 45
MAX_DESCRIPTION_CHARS = 60_000
_WORK_TYPES = {"remote", "onsite", "hybrid"}
_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|llp|plc|gmbh|corp|corporation|co)\b"
)


@dataclass
class IngestResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    new_job_ids: list[int] = field(default_factory=list)  # canonical jobs that deserve extraction
    changed_job_ids: list[int] = field(default_factory=list)  # updated jobs whose text changed


# --- helpers ----------------------------------------------


def _slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def normalise_company(company: str) -> str:
    return re.sub(r"\s+", " ", _COMPANY_SUFFIXES.sub(" ", _slug(company))).strip()


def make_dedupe_key(company: str, title: str) -> str:
    """`slug(company):slug(title)`, case, accents, punctuation and "remote" noise removed."""
    cleaned_title = re.sub(r"\bremote\b", " ", _slug(title))
    title_part = "-".join(cleaned_title.split())
    return f"{normalise_company(company).replace(' ', '-')}:{title_part}"


def _http_url(value: str) -> str:
    value = (value or "").strip()
    return value[:1000] if value.lower().startswith(("http://", "https://")) else ""


def _json_safe(value: Any) -> Any:
    """Round-trip through JSON so datetimes and other objects in `raw` become plain values."""
    return json.loads(json.dumps(value, default=str))


def _normalise_work_type(value: str) -> str:
    cleaned = (value or "").strip().lower().replace("-", "").replace(" ", "")
    return cleaned if cleaned in _WORK_TYPES else Job.WorkType.UNKNOWN


def _derived_fields(raw: RawJob) -> dict[str, Any]:
    """Everything computed from a RawJob: sanitised text, pay, eligibility, flags, dedupe key."""
    description_html = clean_html(raw.description_html) if raw.description_html else ""
    text = (raw.description_text or html_to_text(description_html))[:MAX_DESCRIPTION_CHARS]
    pay = parse_pay(raw.pay_text)
    eligibility = parse_locations(raw.locations)
    return {
        "url": _http_url(raw.url),
        "apply_url": _http_url(raw.apply_url),
        "title": raw.title.strip()[:250],
        "company": raw.company.strip()[:200],
        "description_html": description_html[:MAX_DESCRIPTION_CHARS],
        "description_text": text,
        "pay_text": raw.pay_text.strip()[:300],
        "pay_min": pay.minimum,
        "pay_max": pay.maximum,
        "pay_currency": pay.currency,
        "pay_period": pay.period,
        "locations_raw": [loc.strip() for loc in raw.locations if loc and loc.strip()],
        "eligible_countries": eligibility.countries,
        "eligible_regions": eligibility.regions,
        "is_global": eligibility.is_global,
        "eligibility_known": eligibility.known,
        "work_type": _normalise_work_type(raw.work_type),
        "employment_type": raw.employment_type.strip()[:30],
        "posted_at": raw.posted_at,
        "dedupe_key": make_dedupe_key(raw.company, raw.title),
        "scam_flags": detect_scam_flags(
            text, pay_max=pay.maximum, pay_currency=pay.currency, pay_period=pay.period
        ),
        "raw": _json_safe(raw.raw),
    }


# Fields the source may overwrite only when it actually provides a value, so data filled in from
# the LLM extraction (pay, locations, work type) is not wiped by a later, emptier fetch.
_KEEP_IF_EMPTY = {
    "pay_text", "pay_min", "pay_max", "pay_currency", "pay_period", "locations_raw",
    "eligible_countries", "eligible_regions", "is_global", "eligibility_known", "work_type",
    "employment_type", "apply_url", "posted_at", "description_html",
}  # fmt: skip


def _is_empty(value: Any) -> bool:
    """A value the source did not really provide: blank, null, an empty list, False or 'unknown'."""
    if value is None or value is False or value == []:
        return True
    return isinstance(value, str) and value in ("", Job.WorkType.UNKNOWN, Job.PayPeriod.UNKNOWN)


# --- ingestion ----------------------------------------------


def ingest_raw_jobs(
    source: Source, raw_jobs: list[RawJob], *, now: datetime.datetime | None = None
) -> IngestResult:
    """Upsert each RawJob as a `Job`. One bad job is recorded and skipped, never fatal."""
    now = now or timezone.now()
    result = IngestResult()
    for raw in raw_jobs:
        if not (raw.external_id and raw.title.strip() and raw.company.strip()):
            result.errors.append(f"invalid_raw_job:{(raw.external_id or '?')[:60]}")
            continue
        try:
            with transaction.atomic():
                outcome, job = _upsert(source, raw, now)
        except DatabaseError:
            log_event(
                logger, logging.WARNING, "job_ingest_failed", "A job could not be saved",
                source=source.name, external_id=raw.external_id[:60], exc_info=True,
            )  # fmt: skip
            result.errors.append(f"save_failed:{raw.external_id[:60]}")
            continue
        if outcome == "new":
            result.new += 1
            if job.duplicate_of_id is None:
                result.new_job_ids.append(job.pk)
            else:
                result.duplicates += 1
        elif outcome == "updated":
            result.updated += 1
        elif outcome == "text_changed":
            result.updated += 1
            result.changed_job_ids.append(job.pk)
        else:
            result.unchanged += 1
    log_event(
        logger, logging.INFO, "jobs_ingested", "Jobs ingested", source=source.name,
        new=result.new, updated=result.updated, unchanged=result.unchanged,
        duplicates=result.duplicates, errors=len(result.errors),
    )  # fmt: skip
    return result


def _upsert(source: Source, raw: RawJob, now: datetime.datetime) -> tuple[str, Job]:
    fields = _derived_fields(raw)
    existing = (
        Job.objects.select_for_update().filter(source=source, external_id=raw.external_id).first()
    )
    if existing is None:
        job = Job.objects.create(
            source=source,
            external_id=raw.external_id,
            origin=Job.Origin.SCRAPED,
            visibility=Job.Visibility.PUBLIC,
            first_seen_at=now,
            last_seen_at=now,
            **fields,
        )
        _link_duplicate(job, source, now)
        return "new", job

    changed: list[str] = []
    text_changed = existing.description_text != fields["description_text"]
    for name, value in fields.items():
        if name in _KEEP_IF_EMPTY and _is_empty(value):
            continue
        if getattr(existing, name) != value:
            setattr(existing, name, value)
            changed.append(name)
    existing.last_seen_at = now
    if existing.status != Job.Status.ACTIVE:
        existing.status = Job.Status.ACTIVE
        changed.append("status")
    if text_changed:
        existing.extracted = None  # stale: extract again from the new text
        existing.extracted_at = None
        changed.append("extracted")
    existing.save()  # always: last_seen_at moved
    if text_changed:
        return "text_changed", existing
    return ("updated" if changed else "unchanged"), existing


def _link_duplicate(job: Job, source: Source, now: datetime.datetime) -> None:
    """Cross-source dedupe (§7.4): mark `job` as a duplicate of an older active job elsewhere."""
    if not job.dedupe_key or job.dedupe_key == ":":
        return
    window = now - datetime.timedelta(days=DEDUPE_WINDOW_DAYS)
    canonical = (
        Job.objects.canonical()
        .active()
        .filter(dedupe_key=job.dedupe_key, first_seen_at__gte=window)
        .exclude(pk=job.pk)
        .exclude(source=source)
        .order_by("first_seen_at", "id")
        .first()
    )
    if canonical is None:
        return
    job.duplicate_of = canonical
    job.save(update_fields=["duplicate_of", "updated_at"])
    alternates = list(canonical.alternate_urls)
    if job.url and job.url not in alternates and job.url != canonical.url:
        alternates.append(job.url)
    canonical.alternate_urls = alternates
    # Prefer the platform's own link over an aggregator's: a source whose name matches the job's
    # company is the platform itself.
    new_is_platform = normalise_company(source.display_name) == normalise_company(job.company)
    canonical_source = canonical.source
    canonical_is_platform = canonical_source is not None and normalise_company(
        canonical_source.display_name
    ) == normalise_company(canonical.company)
    if new_is_platform and not canonical_is_platform and (job.apply_url or job.url):
        canonical.apply_url = job.apply_url or job.url
    canonical.save(update_fields=["alternate_urls", "apply_url", "updated_at"])
    log_event(
        logger, logging.INFO, "job_deduplicated", "Duplicate job linked",
        job_id=job.pk, canonical_id=canonical.pk,
    )  # fmt: skip


def touch_seen(
    source: Source, external_ids: set[str], *, now: datetime.datetime | None = None
) -> int:
    """Mark jobs that appeared in a listing but were not re-fetched as seen (and active again)."""
    if not external_ids:
        return 0
    return Job.objects.filter(source=source, external_id__in=external_ids).update(
        last_seen_at=now or timezone.now(), status=Job.Status.ACTIVE
    )


# --- manual jobs (spec §8) -----------------------------------------------------


def ingest_manual_job(
    raw: RawJob,
    *,
    origin: str,
    visibility: str,
    owner: Any = None,
    external_id: str = "",
    now: datetime.datetime | None = None,
) -> tuple[Job, bool]:
    """Create a job that has no source (manual URL, pasted text, email alert). Returns
    `(job, created)`. A public job with the same `external_id` and origin (an email alert for a
    posting we already have) is reused.

    A *public* job that is already in the pool (same URL, or the same company and title seen in the
    last 30 days) is reused instead of duplicated. Private jobs are never matched against other
    people's jobs, so nothing leaks between users.
    """
    now = now or timezone.now()
    fields = _derived_fields(raw)
    if visibility == Job.Visibility.PUBLIC:
        if external_id:
            same = Job.objects.filter(
                source__isnull=True, origin=origin, external_id=external_id,
                visibility=Job.Visibility.PUBLIC,
            ).first()  # fmt: skip
            if same is not None:
                Job.objects.filter(pk=same.pk).update(last_seen_at=now, status=Job.Status.ACTIVE)
                return same, False
        existing = _find_public_job(fields["url"], fields["dedupe_key"], now)
        if existing is not None:
            Job.objects.filter(pk=existing.pk).update(last_seen_at=now, status=Job.Status.ACTIVE)
            return existing, False
    job = Job.objects.create(
        source=None,
        external_id=external_id,
        origin=origin,
        visibility=visibility,
        owner=owner,
        first_seen_at=now,
        last_seen_at=now,
        **fields,
    )
    return job, True


def _find_public_job(url: str, dedupe_key: str, now: datetime.datetime) -> Job | None:
    public = Job.objects.canonical().filter(visibility=Job.Visibility.PUBLIC)
    if url:
        by_url = public.filter(url=url).first()
        if by_url is not None:
            return by_url
    if dedupe_key and dedupe_key != ":":
        window = now - datetime.timedelta(days=DEDUPE_WINDOW_DAYS)
        return public.active().filter(dedupe_key=dedupe_key, first_seen_at__gte=window).first()
    return None


# --- expiry ----------------------------------------------


def expire_by_age(now: datetime.datetime | None = None) -> int:
    """Expire active jobs not seen for N days (spec §7.5). Returns how many were expired.

    Scraped jobs use their source's `expire_after_days` (default `JOB_EXPIRY_DAYS`, 7); manual and
    email jobs expire after 45 days unless seen again.
    """
    now = now or timezone.now()
    expired = 0
    for source in Source.objects.all():
        days = int(source.config.get("expire_after_days", settings.JOB_EXPIRY_DAYS))
        expired += (
            Job.objects.active()
            .filter(source=source, last_seen_at__lt=now - datetime.timedelta(days=days))
            .update(status=Job.Status.EXPIRED)
        )
    expired += (
        Job.objects.active()
        .filter(
            source__isnull=True,
            last_seen_at__lt=now - datetime.timedelta(days=MANUAL_JOB_EXPIRY_DAYS),
        )
        .update(status=Job.Status.EXPIRED)
    )
    log_event(logger, logging.INFO, "job_expired_batch", "Stale jobs expired", count=expired)
    return expired


# --- LLM extraction (spec §11.6) ----------------------------------------------


def _truncate(items: list[str], limit: int) -> list[str]:
    return [item.strip() for item in items if item and item.strip()][:limit]


def extract_job_details(job_id: int) -> bool:
    """Run the once-per-job, system-paid LLM extraction and fill gaps in the job. Idempotent.

    Returns True if the job now has extracted data. Quota and LLM failures are logged and leave
    the job as it was; the job is still usable without extraction.
    """
    job = Job.objects.filter(pk=job_id).first()
    if job is None or job.extracted_at is not None:
        return job is not None and job.extracted_at is not None
    if job.duplicate_of_id is not None:
        return False  # the canonical copy is extracted; duplicates are never matched
    posting = f"{job.title}\n{job.company}\n\n{job.description_text}"
    digest = hashlib.sha256(posting.encode()).hexdigest()[:16]  # stable across processes
    try:
        result = call_structured(
            task=LLMCall.Task.EXTRACT_JOB,
            prompt_name="extract_job",
            variables={"posting": wrap_untrusted("job_posting", posting, MAX_JOB_CHARS)},
            schema=JobExtraction,
            user=None,
            cache_key=f"job:{job.pk}:{digest}",
        )
    except QuotaExceeded:
        log_event(
            logger, logging.WARNING, "job_extraction_skipped", "System cap reached", job_id=job.pk
        )
        return False
    except LLMError:
        log_event(
            logger, logging.ERROR, "job_extraction_failed", "Job extraction failed",
            job_id=job.pk, exc_info=True,
        )  # fmt: skip
        return False

    extracted = result.model_dump()
    extracted["required_skills"] = _truncate(result.required_skills, 15)
    extracted["nice_to_have_skills"] = _truncate(result.nice_to_have_skills, 10)
    extracted["responsibilities"] = _truncate(result.responsibilities, 8)
    extracted["requirements"] = _truncate(result.requirements, 8)
    job.extracted = extracted
    job.extracted_at = timezone.now()
    fill_from_extraction(job, result)
    job.save()
    return True


def fill_from_extraction(job: Job, result: JobExtraction) -> None:
    """Fill pay, locations and work type the source did not provide, then re-normalise (§11.6)."""
    if not job.pay_text and result.pay_text:
        pay = parse_pay(result.pay_text)
        job.pay_text = result.pay_text[:300]
        job.pay_min, job.pay_max = pay.minimum, pay.maximum
        job.pay_currency, job.pay_period = pay.currency, pay.period
    if not job.locations_raw and result.locations:
        eligibility = parse_locations(result.locations)
        job.locations_raw = [loc[:120] for loc in result.locations][:10]
        job.eligible_countries = eligibility.countries
        job.eligible_regions = eligibility.regions
        job.is_global = eligibility.is_global
        job.eligibility_known = eligibility.known
    if job.work_type == Job.WorkType.UNKNOWN and result.work_type != "unknown":
        job.work_type = result.work_type
    flags = detect_scam_flags(
        job.description_text,
        pay_max=job.pay_max if isinstance(job.pay_max, Decimal) else None,
        pay_currency=job.pay_currency,
        pay_period=job.pay_period if job.pay_period != UNKNOWN else "",
    )
    if result.red_flags:
        flags.append("llm_red_flag")
    job.scam_flags = sorted(set(flags))
