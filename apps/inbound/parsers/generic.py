"""Any other job-alert email (spec §9.2). Links to a configured source's detail pages are read
through that source's own parser; otherwise the LLM lists the jobs in the email text. One model
call per email at most, paid by the system. The model may only use links found in the email."""

import hashlib
import logging
import re

from selectolax.lexbor import LexborHTMLParser

from apps.accounts.models import User
from apps.core.logging import log_event
from apps.inbound.mailbox import MailMessage
from apps.jobs import services as job_services
from apps.jobs.models import Job
from apps.llm.client import call_structured
from apps.llm.exceptions import LLMError
from apps.llm.models import LLMCall
from apps.llm.schemas import EmailJobs
from apps.llm.untrusted import MAX_EMAIL_CHARS, wrap_untrusted
from apps.sources import http as source_http
from apps.sources.exceptions import SourceError
from apps.sources.generic.llm_extract import page_text
from apps.sources.models import Source
from apps.sources.registry import fetcher_class

from . import MAX_JOBS_PER_EMAIL, ParsedJob, ParseResult

logger = logging.getLogger(__name__)

MAX_LINKS = 80
MAX_SOURCE_FETCHES = 5  # detail pages read through a source, per email
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_REMOTE = re.compile(r"\bremote\b", re.I)


def collect_links(message: MailMessage) -> list[str]:
    """Every http(s) link in the email, in order, without duplicates."""
    found: list[str] = []
    if message.html:
        for anchor in LexborHTMLParser(message.html).css("a[href]"):
            found.append((anchor.attributes.get("href") or "").strip())
    found += _URL_RE.findall(message.text or "")
    unique: list[str] = []
    for link in found:
        if link.lower().startswith(("http://", "https://")) and link not in unique:
            unique.append(link.rstrip(".,;"))
    return unique[:MAX_LINKS]


def email_text(message: MailMessage) -> str:
    text = message.text.strip()
    if len(text) < 100 and message.html:
        text = page_text(message.html)
    return text


class GenericAlertParser:
    name = "generic"

    def matches(self, message: MailMessage) -> bool:
        return True

    def parse(self, message: MailMessage, user: User) -> ParseResult:
        links = collect_links(message)
        existing = self._from_sources(links)
        if existing:
            return ParseResult(existing=existing)
        return ParseResult(jobs=self._from_llm(message, links))

    # --- links a configured source can read ------------------------------------------------------

    @staticmethod
    def _from_sources(links: list[str]) -> list[Job]:
        sources = list(Source.objects.filter(enabled=True))
        jobs: list[Job] = []
        fetched = 0
        for link in links:
            for source in sources:
                try:
                    external_id = fetcher_class(source)(source, None).match_url(link)  # type: ignore[arg-type]
                except SourceError:
                    continue
                if not external_id:
                    continue
                known = Job.objects.filter(source=source, external_id=external_id).first()
                if known is not None:
                    jobs.append(known)
                    break
                if fetched >= MAX_SOURCE_FETCHES:
                    break
                fetched += 1
                job = GenericAlertParser._read_through(source, external_id, link)
                if job is not None:
                    jobs.append(job)
                break
        return jobs[:MAX_JOBS_PER_EMAIL]

    @staticmethod
    def _read_through(source: Source, external_id: str, link: str) -> Job | None:
        try:
            with source_http.build_client() as client:
                fetcher = fetcher_class(source)(source, client)
                remember = getattr(fetcher, "_urls", None)
                if isinstance(remember, dict):
                    remember[external_id] = link
                raw = fetcher.fetch_detail(external_id)
        except SourceError:
            log_event(
                logger, logging.WARNING, "inbound_source_read_failed",
                "A linked job could not be read", source=source.name,
            )  # fmt: skip
            return None
        ingested = job_services.ingest_raw_jobs(source, [raw])
        job = Job.objects.filter(source=source, external_id=external_id).first()
        if job is not None:
            _queue_followups(ingested.new_job_ids)
        return job

    # --- the LLM ------------------------------------------------------------

    @staticmethod
    def _from_llm(message: MailMessage, links: list[str]) -> list[ParsedJob]:
        text = email_text(message)
        if len(text) < 40:
            return []
        digest = hashlib.sha256(f"{message.message_id}|{text}".encode()).hexdigest()
        try:
            result = call_structured(
                task=LLMCall.Task.EXTRACT_EMAIL_JOBS,
                prompt_name="extract_email_jobs",
                variables={
                    "email": wrap_untrusted(
                        "email", f"Subject: {message.subject}\n\n{text}", MAX_EMAIL_CHARS
                    ),
                    "links": "\n".join(links[:MAX_LINKS]) or "(none)",
                },
                schema=EmailJobs,
                user=None,  # system-paid, never charged to the receiving user
                max_output_tokens=2500,
                cache_key=f"email-jobs:{digest[:32]}",
            )
        except LLMError:
            log_event(
                logger, logging.WARNING, "inbound_llm_failed",
                "Email could not be read by the model", exc_info=True,
            )  # fmt: skip
            return []
        if not result.is_job_alert:
            return []
        allowed = set(links)
        jobs: list[ParsedJob] = []
        for item in result.jobs[:MAX_JOBS_PER_EMAIL]:
            title, company = item.title.strip(), item.company.strip()
            if not title or not company:
                continue
            url = item.url.strip() if item.url.strip() in allowed else ""
            key = hashlib.sha256(f"{title}|{company}|{url}".lower().encode()).hexdigest()[:16]
            jobs.append(
                ParsedJob(
                    title=title[:250],
                    company=company[:200],
                    url=url,
                    external_id=f"email:{key}",
                    locations=[item.location.strip()[:150]] if item.location.strip() else [],
                    work_type="remote" if _REMOTE.search(item.location) else "",
                    pay_text=(item.pay_text or "")[:300],
                    description_text=(item.summary or "")[:500],
                )
            )
        return jobs


def _queue_followups(job_ids: list[int]) -> None:
    from apps.core.tasks import enqueue
    from apps.jobs.tasks import extract_job_task

    for job_id in job_ids:
        enqueue(extract_job_task, job_id)
