"""Email job-alert ingestion: addresses, routing, parsing, ingesting and retention (spec §9).

Email content is untrusted. Nothing here logs sender addresses, subjects, bodies, job text or
recipient tokens; only IDs, parser names and counts.
"""

import datetime
import functools
import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.logging import log_event
from apps.jobs import services as job_services
from apps.jobs.models import Job
from apps.matching import services as matching_services
from apps.sources.base import RawJob

from .mailbox import PROCESSED, UNROUTED, MailMessage, mailbox_enabled, open_mailbox
from .models import ForwardingConfirmation, InboundAddress, InboundEmail, new_token
from .parsers import ParsedJob, choose_parser
from .parsers.generic import collect_links

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
MAX_BODY_CHARS = 200_000
POLL_LIMIT = 50
SHORT_DESCRIPTION = 200  # below this a job has too little text for LLM extraction to help

_CONFIRMATION_SENDER = re.compile(r"(^|@)(forwarding-noreply@google\.com)$", re.I)
_CONFIRMATION_HOSTS = {"mail-settings.google.com"}
_SUBJECT_CODE = re.compile(r"\(#(\d{6,12})\)")
_BODY_CODE = re.compile(r"confirmation code[^0-9]{0,40}(\d{6,12})", re.I)


# --- addresses --------------------------------------------------------------------------------


def ensure_address(user: User) -> InboundAddress:
    """The user's forwarding address, created on first use. Safe to call repeatedly."""
    existing = InboundAddress.objects.for_user(user).first()
    if existing is not None:
        return existing
    for _ in range(10):
        try:
            with transaction.atomic():
                return InboundAddress.objects.create(user=user, token=new_token())
        except IntegrityError:  # a token clash, or a concurrent create for this user
            found = InboundAddress.objects.for_user(user).first()
            if found is not None:
                return found
    raise RuntimeError("could not create a unique forwarding address")


def find_address(recipients: list[str]) -> InboundAddress | None:
    """The active address any recipient header points at (spec §9.2). The domain must be ours."""
    pattern = re.compile(
        rf"^(u-[a-z0-9]{{4,16}})@{re.escape(settings.INBOUND_EMAIL_DOMAIN.lower())}$"
    )
    for recipient in recipients:
        match = pattern.match(recipient.strip().lower())
        if match:
            address = (
                InboundAddress.objects.select_related("user")
                .filter(token=match.group(1), active=True, user__is_active=True)
                .first()
            )
            if address is not None:
                return address
    return None


# --- Gmail forwarding confirmation ------------------------------------------------------------


def _is_confirmation(message: MailMessage) -> bool:
    return bool(_CONFIRMATION_SENDER.search(message.from_address)) and (
        "forwarding confirmation" in message.subject.lower()
    )


def _confirmation_details(message: MailMessage) -> tuple[str, str] | None:
    """`(link, code)` if the email carries a confirmation link on Google's own host. Any other
    link is ignored, so a forged "confirmation" cannot put an arbitrary address on a banner."""
    link = ""
    for candidate in collect_links(message):
        parts = urlsplit(candidate)
        if parts.scheme == "https" and (parts.hostname or "").lower() in _CONFIRMATION_HOSTS:
            link = candidate
            break
    if not link:
        return None
    code = ""
    for pattern, text in ((_SUBJECT_CODE, message.subject), (_BODY_CODE, message.text)):
        found = pattern.search(text)
        if found:
            code = found.group(1)
            break
    return link[:1000], code


def pending_confirmation(user: User) -> ForwardingConfirmation | None:
    return ForwardingConfirmation.objects.for_user(user).filter(dismissed=False).first()


def dismiss_confirmation(user: User, confirmation_id: int) -> bool:
    return bool(
        ForwardingConfirmation.objects.for_user(user)
        .filter(pk=confirmation_id)
        .update(dismissed=True)
    )


def _record_confirmation(address: InboundAddress, message: MailMessage) -> bool:
    details = _confirmation_details(message)
    if details is None:
        return False
    link, code = details
    confirmation = ForwardingConfirmation.objects.create(
        user=address.user, provider="gmail", confirmation_url=link, confirmation_code=code,
        received_at=message.received_at,
    )  # fmt: skip
    from apps.notifications import services as notification_services
    from apps.notifications.models import Notification

    if Notification.Channel.TELEGRAM in notification_services.channels_for(address.user):
        notification_services.create_notification(
            address.user,
            kind=Notification.Kind.SYSTEM,
            channel=Notification.Channel.TELEGRAM,
            dedupe_key=f"forwarding:{confirmation.pk}",
            payload={"forwarding_confirmation_id": confirmation.pk},
        )
    return True


# --- ingesting parsed jobs --------------------------------------------------------------------


def _ingest(parsed: list[ParsedJob], parser_name: str) -> list[Job]:
    jobs: list[Job] = []
    for item in parsed:
        raw = RawJob(
            source="", external_id=item.external_id, url=item.url, title=item.title,
            company=item.company, locations=item.locations, work_type=item.work_type,
            pay_text=item.pay_text, description_text=item.description_text,
            raw={"origin": "email", "parser": parser_name},
        )  # fmt: skip
        job, created = job_services.ingest_manual_job(
            raw, origin=Job.Origin.EMAIL, visibility=Job.Visibility.PUBLIC,
            external_id=item.external_id,
        )  # fmt: skip
        if created:
            transaction.on_commit(functools.partial(_enqueue_followup, job.pk))
        jobs.append(job)
    return jobs


def _enqueue_followup(job_id: int) -> None:
    """Everyone's rule filter sees the new job. Email jobs carry little text, so extraction is
    only worth its cost when there is a real description."""
    from apps.core.tasks import enqueue
    from apps.jobs.tasks import extract_job_task
    from apps.matching.tasks import match_new_job_task

    job = Job.objects.filter(pk=job_id).first()
    if job is None:
        return
    task = (
        extract_job_task if len(job.description_text) >= SHORT_DESCRIPTION else match_new_job_task
    )
    enqueue(task, job_id)


# --- one message ------------------------------------------------------------------------------


@dataclass
class Outcome:
    folder: str
    status: str = ""
    jobs: int = 0


def _message_key(message: MailMessage) -> str:
    if message.message_id:
        return message.message_id
    digest = hashlib.sha256(
        f"{message.from_address}|{message.subject}|{message.received_at.isoformat()}".encode()
    ).hexdigest()
    return f"no-id:{digest[:40]}"


def process_message(message: MailMessage) -> Outcome:
    """Route, de-duplicate, read and ingest one email. Never raises; returns where to file it."""
    address = find_address(message.recipients)
    if address is None:
        log_event(logger, logging.INFO, "inbound_unrouted", "Email not addressed to a user")
        return Outcome(UNROUTED, "unrouted")
    key = _message_key(message)
    try:
        with transaction.atomic():
            record = InboundEmail.objects.create(
                user=address.user, address=address, message_id=key[:300],
                from_address=message.from_address[:254], subject=message.subject[:300],
                received_at=message.received_at, body_text=message.text[:MAX_BODY_CHARS],
                body_html=message.html[:MAX_BODY_CHARS],
            )  # fmt: skip
    except IntegrityError:
        log_event(logger, logging.INFO, "inbound_duplicate", "Email already processed")
        return Outcome(PROCESSED, "duplicate")

    try:
        outcome = _handle(address, record, message)
    except Exception:
        InboundEmail.objects.filter(pk=record.pk).update(
            status=InboundEmail.Status.FAILED, error="processing_error"
        )
        log_event(
            logger, logging.ERROR, "inbound_failed", "Email could not be processed",
            inbound_email_id=record.pk, exc_info=True,
        )  # fmt: skip
        return Outcome(PROCESSED, "failed")
    return outcome


def _handle(address: InboundAddress, record: InboundEmail, message: MailMessage) -> Outcome:
    user = address.user
    if _is_confirmation(message):
        found = _record_confirmation(address, message)
        InboundEmail.objects.filter(pk=record.pk).update(
            parser="gmail_confirmation",
            status=InboundEmail.Status.PARSED if found else InboundEmail.Status.IGNORED,
        )
        log_event(
            logger, logging.INFO, "inbound_confirmation", "Forwarding confirmation received",
            inbound_email_id=record.pk, recorded=found,
        )  # fmt: skip
        return Outcome(PROCESSED, "confirmation")

    parser = choose_parser(message)
    result = parser.parse(message, user)
    jobs = [*_ingest(result.jobs, parser.name), *result.existing]
    seen: set[int] = set()
    for job in jobs:
        if job.pk in seen:
            continue
        seen.add(job.pk)
        matching_services.match_job_for_user(user, job)  # the receiving user first
    InboundEmail.objects.filter(pk=record.pk).update(
        parser=parser.name,
        jobs_found=len(seen),
        status=InboundEmail.Status.PARSED if seen else InboundEmail.Status.IGNORED,
    )
    log_event(
        logger, logging.INFO, "inbound_parsed", "Email read",
        inbound_email_id=record.pk, parser=parser.name, jobs=len(seen),
    )  # fmt: skip
    return Outcome(PROCESSED, "parsed", len(seen))


# --- polling and retention --------------------------------------------------------------------


def poll_mailbox(limit: int = POLL_LIMIT) -> dict[str, int]:
    """Read unseen mail from the dedicated inbox and file each message (spec §9.1)."""
    counts = {"messages": 0, "parsed": 0, "jobs": 0, "unrouted": 0, "duplicates": 0, "failed": 0}
    if not mailbox_enabled():
        return counts
    client = open_mailbox()
    if client is None:
        return counts
    try:
        for message in client.fetch_unseen(limit):
            outcome = process_message(message)
            counts["messages"] += 1
            counts["jobs"] += outcome.jobs
            key = {"parsed": "parsed", "unrouted": "unrouted", "duplicate": "duplicates",
                   "failed": "failed"}.get(outcome.status)  # fmt: skip
            if key:
                counts[key] += 1
            try:
                client.finish(message.uid, outcome.folder)
            except Exception:
                log_event(
                    logger, logging.ERROR, "inbound_file_failed", "Email could not be filed",
                    exc_info=True,
                )  # fmt: skip
    finally:
        client.close()
    return counts


def clear_old_bodies(now: datetime.datetime | None = None) -> int:
    """Delete the stored text of emails older than 30 days (spec §9.3). Metadata stays."""
    cutoff = (now or timezone.now()) - datetime.timedelta(days=RETENTION_DAYS)
    return (
        InboundEmail.objects.filter(received_at__lt=cutoff)
        .exclude(body_text="", body_html="")
        .update(body_text="", body_html="")
    )


def recent_emails(user: User, limit: int = 15) -> list[InboundEmail]:
    return list(InboundEmail.objects.for_user(user)[:limit])
