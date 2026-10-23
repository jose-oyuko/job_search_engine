"""Manual job input: paste a URL or a job description (spec §8).

`add_job_from_url` works through, in order: blocked sites, a configured source's own parser, the
page's JSON-LD, and finally LLM extraction. `add_job_from_text` creates a private job from pasted
text. Both return the job to show, and call the matching hook (a no-op until Phase 6).

Pasted text and fetched pages are untrusted (spec §11.5): they are wrapped before reaching the LLM,
and nothing here logs URLs, page text or pasted text.
"""

import functools
import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.logging import log_event
from apps.core.ssrf import (
    FetchFailed,
    Resolver,
    UnsafeURL,
    safe_fetch,
    validate_public_url,
)
from apps.core.tasks import enqueue
from apps.llm.client import call_structured
from apps.llm.exceptions import LLMError, QuotaExceeded
from apps.llm.models import LLMCall
from apps.llm.schemas import ExtractedPage, JobExtraction
from apps.llm.untrusted import MAX_JOB_CHARS, wrap_untrusted
from apps.sources import http as source_http
from apps.sources.base import RawJob
from apps.sources.exceptions import SourceError
from apps.sources.generic.jsonld import find_job_posting, posting_to_rawjob
from apps.sources.generic.llm_extract import page_text
from apps.sources.models import Source
from apps.sources.registry import fetcher_class

from . import matching_hook, services
from .models import Job

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS = 80
MAX_TEXT_CHARS = 30_000
MAX_URL_CHARS = 2000

# Sites that block automated reading. A page on any of these (or a subdomain) is refused with a
# message asking the user to paste the description instead (spec §8.1).
BLOCKED_DOMAINS = (
    "linkedin.com",
    "lnkd.in",
    "indeed.com",
    "glassdoor.com",
    "facebook.com",
    "fb.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
)
BLOCKED_MESSAGE = "This site can't be read automatically. Paste the job description instead."

_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_.*|ref|ref_src|source|trk|igshid)$", re.I)


class ManualJobError(Exception):
    """The job could not be added. `message` is safe to show the user."""

    def __init__(self, message: str, reason: str = "failed") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason

    def __str__(self) -> str:
        return self.message


class BlockedSite(ManualJobError):
    def __init__(self) -> None:
        super().__init__(BLOCKED_MESSAGE, "blocked_site")


class NeedsDetails(ManualJobError):
    """The pasted text does not say which role or employer it is; the user must provide it."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            "We could not tell the job title or company from that text. "
            "Please fill in the missing details below.",
            "needs_details",
        )
        self.missing = missing


@dataclass(frozen=True)
class ManualResult:
    job: Job
    created: bool
    path: str  # known_source | jsonld | llm | text | existing


# --- URL helpers ----------------------------------------------


def normalise_input_url(value: str) -> str:
    """Trim, add https:// to a bare domain, and bound the length."""
    url = value.strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise ManualJobError("Please enter a valid web address.", "bad_url")
    if "://" not in url:
        url = "https://" + url
    return url


def canonical_url(url: str) -> str:
    """The URL without fragment, tracking parameters, default port or trailing noise, so the same
    listing pasted twice is recognised."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    default_port = 443 if parts.scheme.lower() == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)])
    path = parts.path if parts.path != "/" else ""
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def is_blocked(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in BLOCKED_DOMAINS)


def _enqueue_extraction(job_id: int) -> None:
    from .tasks import extract_job_task

    # After commit so the worker sees the row; system-paid structure extraction (spec §7.6).
    transaction.on_commit(functools.partial(enqueue, extract_job_task, job_id))


# --- paste a URL ----------------------------------------------


def add_job_from_url(
    user: User,
    url: str,
    *,
    resolver: Resolver | None = None,
    client: httpx.Client | None = None,
) -> ManualResult:
    """Add a job from a link. Raises `ManualJobError` (including `BlockedSite`) for problems the
    user can act on, `QuotaExceeded` when the LLM allowance is used up."""
    # Check the address as typed (credentials, odd IP spellings...) before tidying it up.
    typed = normalise_input_url(url)
    if is_blocked(typed):
        _log_block("blocked_site")
        raise BlockedSite
    try:
        validate_public_url(typed, resolver)
    except UnsafeURL as exc:
        _log_block(exc.reason)
        raise ManualJobError(UnsafeURL.message, "unsafe_url") from exc
    url = canonical_url(typed)

    existing = Job.objects.visible_to(user).canonical().filter(url=url).first()
    if existing is not None:
        return _done(user, ManualResult(existing, False, "existing"))

    result = _from_known_source(url)
    if result is None:
        result = _from_page(user, url, resolver=resolver, client=client)
    return _done(user, result)


def _refuse_blocked(url: str) -> None:
    """Called before every request, including redirect hops: a link that bounces to a blocked site
    is refused without ever contacting it."""
    if is_blocked(url):
        _log_block("blocked_site")
        raise BlockedSite


def _log_block(reason: str) -> None:
    log_event(logger, logging.WARNING, "ssrf_blocked", "Manual URL refused", reason=reason)


def _done(user: User, result: ManualResult) -> ManualResult:
    log_event(
        logger, logging.INFO, "manual_job_added", "Manual job added",
        job_id=result.job.pk, path=result.path, created=result.created,
    )  # fmt: skip
    matching_hook.job_added_for_user(user, result.job)
    return result


def _from_known_source(url: str) -> ManualResult | None:
    """Use a configured source's own parser when the URL is one of its detail pages."""
    for source in Source.objects.filter(enabled=True):
        try:
            matcher = fetcher_class(source)(source, None)  # type: ignore[arg-type]
            external_id = matcher.match_url(url)
        except SourceError:
            continue
        if not external_id:
            continue
        existing = Job.objects.filter(source=source, external_id=external_id).first()
        if existing is not None:
            return ManualResult(existing, False, "existing")
        with source_http.build_client() as http_client:
            fetcher = fetcher_class(source)(source, http_client)
            remember = getattr(fetcher, "_urls", None)
            if isinstance(remember, dict):
                remember[external_id] = url
            try:
                raw = fetcher.fetch_detail(external_id)
            except SourceError as exc:
                raise ManualJobError(
                    "We could not read that listing right now. Please try again later.",
                    "source_error",
                ) from exc
        ingested = services.ingest_raw_jobs(source, [raw])
        job = Job.objects.get(source=source, external_id=external_id)
        for job_id in ingested.new_job_ids:
            _enqueue_extraction(job_id)
        return ManualResult(job, ingested.new > 0, "known_source")
    return None


def _from_page(
    user: User, url: str, *, resolver: Resolver | None, client: httpx.Client | None
) -> ManualResult:
    try:
        page = safe_fetch(url, resolver=resolver, client=client, before_request=_refuse_blocked)
    except UnsafeURL as exc:
        _log_block(exc.reason)
        raise ManualJobError(UnsafeURL.message, "unsafe_url") from exc
    except FetchFailed as exc:
        raise ManualJobError(exc.message, exc.reason) from exc

    final_url = canonical_url(page.final_url)
    if is_blocked(final_url):  # a short link that redirected to a blocked site
        _log_block("blocked_after_redirect")
        raise BlockedSite
    if final_url != url:
        existing = Job.objects.visible_to(user).canonical().filter(url=final_url).first()
        if existing is not None:
            return ManualResult(existing, False, "existing")

    posting = find_job_posting(page.text)
    if posting is not None:
        try:
            raw = posting_to_rawjob(posting, source="", external_id="", url=final_url)
        except SourceError as exc:
            raise ManualJobError(
                "That page does not look like a job posting.", "not_a_job"
            ) from exc
        raw.raw = {"origin": "jsonld"}
        return _create_public(raw, "jsonld")

    return _create_public(_extract_page(user, page.text, final_url), "llm")


def _extract_page(user: User, html: str, url: str) -> RawJob:
    text = page_text(html)
    if len(text) < MIN_TEXT_CHARS:
        raise ManualJobError("We could not find any readable text on that page.", "empty_page")
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    try:
        page = call_structured(
            task=LLMCall.Task.EXTRACT_PAGE,
            prompt_name="extract_page",
            variables={"page": wrap_untrusted("page", text, MAX_JOB_CHARS)},
            schema=ExtractedPage,
            user=user,  # counts towards the user's monthly manual extractions
            cache_key=f"page:manual:{digest}",
        )
    except QuotaExceeded:
        raise
    except LLMError as exc:
        raise ManualJobError(
            "We could not read that page right now. Try again, or paste the description instead.",
            "llm_error",
        ) from exc
    if not page.is_job_posting or not page.title:
        raise ManualJobError("That page does not look like a single job posting.", "not_a_job")
    return RawJob(
        source="",
        external_id="",
        url=url,
        title=page.title,
        company=page.company or (urlsplit(url).hostname or ""),
        apply_url=page.apply_url or "",
        description_text=page.description,
        pay_text=page.pay_text or "",
        locations=page.locations,
        work_type="" if page.work_type == "unknown" else page.work_type,
        employment_type=page.employment_type or "",
        raw={"origin": "llm", "text_sha": digest},
    )


def _create_public(raw: RawJob, path: str) -> ManualResult:
    job, created = services.ingest_manual_job(
        raw, origin=Job.Origin.MANUAL_URL, visibility=Job.Visibility.PUBLIC
    )
    if created:
        _enqueue_extraction(job.pk)
    return ManualResult(job, created, path if created else "existing")


# --- paste a description ----------------------------------------------


def add_job_from_text(
    user: User, text: str, *, title: str = "", company: str = "", url: str = ""
) -> ManualResult:
    """Create a private job from pasted text. The LLM reads it (counting towards the user's manual
    extractions); the user's own title, company and link, when given, take precedence."""
    text = text.replace("\x00", "").strip()
    if len(text) < MIN_TEXT_CHARS:
        raise ManualJobError(
            f"That is too short to be a job description (at least {MIN_TEXT_CHARS} characters).",
            "too_short",
        )
    if len(text) > MAX_TEXT_CHARS:
        raise ManualJobError(
            f"That is too long. Please paste at most {MAX_TEXT_CHARS:,} characters.", "too_long"
        )
    title, company = title.strip()[:250], company.strip()[:200]
    link = _hint_url(url)
    digest = hashlib.sha256(text.encode()).hexdigest()

    existing = Job.objects.filter(
        owner=user, origin=Job.Origin.MANUAL_TEXT, raw__text_sha=digest
    ).first()
    if existing is not None:
        return _done(user, ManualResult(existing, False, "existing"))

    hints = "".join(
        f"{label}: {value}\n" for label, value in (("Title", title), ("Company", company)) if value
    )
    posting = f"{hints}\n{text}" if hints else text
    try:
        extraction = call_structured(
            task=LLMCall.Task.EXTRACT_JOB,
            prompt_name="extract_job",
            variables={"posting": wrap_untrusted("job_posting", posting, MAX_JOB_CHARS)},
            schema=JobExtraction,
            user=user,  # counts towards the user's monthly manual extractions
            cache_key=f"manual-text:{user.pk}:{digest[:32]}",
        )
    except QuotaExceeded:
        raise
    except LLMError as exc:
        raise ManualJobError(
            "We could not analyse that text right now. Please try again in a moment.", "llm_error"
        ) from exc

    title = title or (extraction.title or "").strip()[:250]
    company = company or (extraction.company or "").strip()[:200]
    missing = [name for name, value in (("title", title), ("company", company)) if not value]
    if missing:
        raise NeedsDetails(missing)

    raw = RawJob(
        source="",
        external_id="",
        url=link,
        title=title,
        company=company,
        description_text=text,
        raw={"origin": "text", "text_sha": digest},
    )
    job, _ = services.ingest_manual_job(
        raw, origin=Job.Origin.MANUAL_TEXT, visibility=Job.Visibility.PRIVATE, owner=user
    )
    job.extracted = extraction.model_dump()
    job.extracted_at = timezone.now()
    services.fill_from_extraction(job, extraction)
    job.save()
    return _done(user, ManualResult(job, True, "text"))


def _hint_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I) or len(value) > MAX_URL_CHARS:
        raise ManualJobError("The link must start with http:// or https://", "bad_link")
    return canonical_url(value)
