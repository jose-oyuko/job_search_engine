"""Accounts business logic: invites, signup completion and the waitlist.

Views, adapters, signal handlers and the admin only call into here. Invite codes and email
addresses are credentials / personal data, so log lines carry record IDs only.
"""

import datetime
import functools
import logging
import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.core.logging import log_event
from apps.inbound import services as inbound_services
from apps.llm.models import UsageQuota

from .exceptions import (
    InviteAlreadyUsed,
    InviteEmailMismatch,
    InviteExpired,
    InviteNotFound,
)
from .models import ConsentRecord, Invite, User, UserSettings, WaitlistEntry

logger = logging.getLogger(__name__)

INVITE_SESSION_KEY = "invite_code"


def validate_invite(code: str, email: str | None = None) -> Invite:
    """Return the invite if it can be used now, else raise an `InviteError` subclass.

    When `email` is given and the invite is locked to an address, the two must match.
    """
    invite = Invite.objects.filter(code=code).first() if code else None
    if invite is None:
        raise InviteNotFound
    if invite.is_used:
        raise InviteAlreadyUsed
    if invite.is_expired:
        raise InviteExpired
    if email is not None and invite.email and invite.email.lower() != email.strip().lower():
        raise InviteEmailMismatch
    return invite


def invite_url(invite: Invite) -> str:
    path: str = reverse("accounts:join", args=[invite.code])
    return str(settings.SITE_URL).rstrip("/") + path


def generate_invites(
    *,
    created_by: User | None,
    count: int = 1,
    email: str = "",
    note: str = "",
    expires_in_days: int | None = None,
) -> list[Invite]:
    """Create `count` invites. An email lock only makes sense for a single invite."""
    if count < 1:
        raise ValueError("count must be at least 1")
    if email and count != 1:
        raise ValueError("An email-locked invite must be generated one at a time.")
    days = settings.INVITE_EXPIRY_DAYS if expires_in_days is None else expires_in_days
    expires_at = timezone.now() + datetime.timedelta(days=days)
    with transaction.atomic():
        invites = [
            Invite.objects.create(
                created_by=created_by,
                email=email.strip().lower(),
                note=note,
                expires_at=expires_at,
            )
            for _ in range(count)
        ]
    log_event(
        logger,
        logging.INFO,
        "invites_generated",
        "Invites generated",
        count=count,
        created_by=created_by.pk if created_by else None,
    )
    return invites


def send_invite_email(invite: Invite, to_email: str) -> bool:
    """Email the invite link. Returns False (and logs) if the mail server refused or was down."""
    context = {
        "invite_url": invite_url(invite),
        "expires_at": invite.expires_at,
        "site_name": settings.SITE_NAME,
    }
    message = EmailMultiAlternatives(
        f"You are invited to {settings.SITE_NAME}",
        render_to_string("accounts/email/invite_message.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [to_email],
    )
    message.attach_alternative(
        render_to_string("accounts/email/invite_message.html", context), "text/html"
    )
    try:
        message.send()
    except (OSError, smtplib.SMTPException):
        log_event(
            logger,
            logging.ERROR,
            "invite_email_failed",
            "Invite email could not be sent",
            invite_id=invite.pk,
            exc_info=True,
        )
        return False
    log_event(logger, logging.INFO, "invite_email_sent", "Invite email sent", invite_id=invite.pk)
    return True


def record_consents(user: User) -> None:
    """Terms and privacy consent at the currently published versions."""
    ConsentRecord.objects.bulk_create(
        [
            ConsentRecord(user=user, kind=ConsentRecord.Kind.TERMS, version=settings.TERMS_VERSION),
            ConsentRecord(
                user=user, kind=ConsentRecord.Kind.PRIVACY, version=settings.PRIVACY_VERSION
            ),
        ]
    )


def _mark_invite_used(invite_code: str, user: User) -> bool:
    # Conditional update: two signups racing on one invite cannot both claim it.
    claimed = Invite.objects.filter(code=invite_code, used_at__isnull=True).update(
        used_by=user, used_at=timezone.now()
    )
    return bool(claimed)


def complete_signup(user: User, invite_code: str | None) -> None:
    """Create everything a new account needs. Idempotent, so a replayed signal is harmless."""
    with transaction.atomic():
        UserSettings.objects.get_or_create(user=user)
        UsageQuota.objects.get_or_create(user=user)
        if not ConsentRecord.objects.for_user(user).exists():
            record_consents(user)
        # TODO(phase 3): create the empty Profile here.
        inbound_services.ensure_address(user)
        invite_claimed = bool(invite_code) and _mark_invite_used(invite_code or "", user)
    log_event(logger, logging.INFO, "user_signed_up", "User signed up", user_id=user.pk)
    if invite_claimed:
        log_event(logger, logging.INFO, "invite_used", "Invite used", user_id=user.pk)
    elif invite_code:
        log_event(
            logger,
            logging.WARNING,
            "invite_claim_lost",
            "Invite was already claimed by another signup",
            user_id=user.pk,
        )


def submit_waitlist(*, name: str, email: str, reason: str) -> tuple[WaitlistEntry, bool]:
    """Save a request for an invite. A repeat request from the same address is not duplicated.

    Returns `(entry, created)`. Callers show the same thank-you either way, so the form cannot
    be used to find out which addresses are already known.
    """
    email = email.strip().lower()
    existing = (
        WaitlistEntry.objects.filter(email__iexact=email)
        .exclude(status=WaitlistEntry.Status.REJECTED)
        .first()
    )
    if existing is not None:
        return existing, False
    entry = WaitlistEntry.objects.create(name=name.strip(), email=email, reason=reason.strip())
    transaction.on_commit(functools.partial(_notify_staff_waitlist, entry.pk))
    log_event(
        logger,
        logging.INFO,
        "waitlist_entry_created",
        "Waitlist request received",
        waitlist_entry_id=entry.pk,
    )
    return entry, True


def _notify_staff_waitlist(entry_id: int) -> None:
    """Tell staff on Telegram about a new request. Never affects the form: errors are logged."""
    try:
        from apps.notifications import services as notification_services

        notification_services.notify_new_waitlist_entry(entry_id)
    except Exception:
        log_event(
            logger, logging.ERROR, "staff_notice_failed", "Staff notice could not be queued",
            waitlist_entry_id=entry_id, exc_info=True,
        )  # fmt: skip


def approve_waitlist_entry(entry: WaitlistEntry, approved_by: User | None) -> tuple[Invite, bool]:
    """Create an email-locked invite for the entry and email it.

    Returns `(invite, emailed)`. If the entry was already approved the existing invite is
    returned and nothing is sent, so a double click cannot spam the applicant.
    """
    if entry.status == WaitlistEntry.Status.INVITED and entry.invite is not None:
        return entry.invite, False
    with transaction.atomic():
        (invite,) = generate_invites(
            created_by=approved_by, email=entry.email, note=f"Waitlist entry #{entry.pk}"
        )
        entry.status = WaitlistEntry.Status.INVITED
        entry.invite = invite
        entry.save(update_fields=["status", "invite", "updated_at"])
    emailed = send_invite_email(invite, entry.email)
    log_event(
        logger,
        logging.INFO,
        "waitlist_entry_approved",
        "Waitlist entry approved",
        waitlist_entry_id=entry.pk,
        invite_id=invite.pk,
        emailed=emailed,
    )
    return invite, emailed
