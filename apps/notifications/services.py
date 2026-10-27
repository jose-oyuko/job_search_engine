"""Notifications: linking, instant alerts, digests, kit delivery, staff notices (spec §13).

Every send goes through `create_notification` (one row per user, channel and `dedupe_key`) and
`deliver` (claims the row, sends, records the outcome), so a retry, a second worker or the
dispatcher running twice can never send the same thing twice. Text is built from live data at
send time and is never logged; only IDs, kinds and counts are.
"""

import datetime
import functools
import logging
import smtplib
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from apps.accounts.models import User, UserSettings
from apps.core.logging import log_event
from apps.kits import documents as kit_documents
from apps.kits import services as kit_services
from apps.kits.models import Kit
from apps.matching.models import Match

from . import render, schedule, signing
from .exceptions import (
    ChatAlreadyLinked,
    GatewayError,
    LinkTokenInvalid,
    TelegramUnavailable,
)
from .gateway import TelegramGateway, get_gateway, telegram_enabled
from .models import Notification, TelegramLink, TelegramLinkToken

logger = logging.getLogger(__name__)

KIT_WAIT = datetime.timedelta(seconds=90)
KIT_POLL_SECONDS = 10
CLAIM_TIMEOUT = datetime.timedelta(minutes=10)
MAX_ATTEMPTS = 3
DIGEST_MIN_SCORE = 40

# Outcomes of `deliver`
SENT, SKIPPED, FAILED, NOT_DUE, RETRY, BUSY, GONE = (
    "sent", "skipped", "failed", "not_due", "retry", "busy", "gone",
)  # fmt: skip

TG, EMAIL = Notification.Channel.TELEGRAM, Notification.Channel.EMAIL


# --- linking Telegram -------------------------------------------------------------------------


def active_link(user: User) -> TelegramLink | None:
    return TelegramLink.objects.filter(user=user, active=True).first()


def create_link_token(user: User) -> TelegramLinkToken:
    """A fresh 15-minute token; any unused earlier one is withdrawn."""
    if not telegram_enabled() or not settings.TELEGRAM_BOT_USERNAME:
        raise TelegramUnavailable("Telegram is not set up on this server yet.")
    TelegramLinkToken.objects.for_user(user).filter(used_at__isnull=True).delete()
    return TelegramLinkToken.objects.create(user=user)


def link_url(token: TelegramLinkToken) -> str:
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={token.token}"


def consume_link_token(token_value: str, chat_id: str, username: str = "") -> TelegramLink:
    """Called by the bot on `/start <token>`: connect this chat to the token's user."""
    with transaction.atomic():
        token = (
            TelegramLinkToken.objects.select_for_update()
            .select_related("user")
            .filter(token=token_value)
            .first()
        )
        if token is None or not token.is_usable():
            raise LinkTokenInvalid(
                "That link has expired. Press Connect Telegram again on the site."
            )
        other = TelegramLink.objects.filter(chat_id=chat_id).exclude(user=token.user).first()
        if other is not None:
            raise ChatAlreadyLinked("This Telegram chat is already connected to another account.")
        link, _ = TelegramLink.objects.update_or_create(
            user=token.user,
            defaults={
                "chat_id": chat_id,
                "username": username[:64],
                "active": True,
                "linked_at": timezone.now(),
            },
        )
        token.used_at = timezone.now()
        token.save(update_fields=["used_at", "updated_at"])
        UserSettings.objects.filter(user=token.user).update(notify_telegram=True)
    log_event(
        logger, logging.INFO, "telegram_linked", "Telegram linked",
        for_user_id=token.user_id, chat=signing.chat_hash(chat_id),
    )  # fmt: skip
    return link


def unlink(user: User) -> None:
    TelegramLink.objects.filter(user=user).delete()
    TelegramLinkToken.objects.for_user(user).filter(used_at__isnull=True).delete()
    UserSettings.objects.filter(user=user).update(notify_telegram=False)
    log_event(logger, logging.INFO, "telegram_unlinked", "Telegram unlinked", for_user_id=user.pk)


def user_for_chat(chat_id: str) -> User | None:
    link = TelegramLink.objects.select_related("user").filter(chat_id=chat_id, active=True).first()
    return link.user if link and link.user.is_active else None


# --- channels and creating notifications -------------------------------------------------------


def _settings(user: User) -> UserSettings:
    row, _ = UserSettings.objects.get_or_create(user=user)
    return row


def channels_for(user: User, row: UserSettings | None = None) -> list[str]:
    """The channels this user wants and that can work right now."""
    row = row or _settings(user)
    channels: list[str] = []
    if row.notify_telegram and active_link(user) is not None and get_gateway() is not None:
        channels.append(TG)
    if row.notify_email and user.email:
        channels.append(EMAIL)
    return channels


def _enqueue_delivery(notification_id: int, *, delay: int = 0) -> None:
    from apps.core.tasks import enqueue

    from .tasks import deliver_notification_task

    if delay > 0:
        deliver_notification_task.schedule(args=(notification_id,), delay=delay)
    else:
        enqueue(deliver_notification_task, notification_id)


def create_notification(
    user: User,
    *,
    kind: str,
    channel: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    deliver_after: datetime.datetime | None = None,
    matches: list[Match] | None = None,
) -> tuple[Notification, bool]:
    """Create the row and queue its delivery. Returns `(notification, created)`; an existing row
    with the same key is returned untouched (`created` False), which is what makes retries safe."""
    now = timezone.now()
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                user=user, kind=kind, channel=channel, dedupe_key=dedupe_key,
                payload=payload or {}, deliver_after=deliver_after or now,
            )  # fmt: skip
    except IntegrityError:
        return Notification.objects.get(user=user, dedupe_key=dedupe_key), False
    if matches:
        notification.matches.set(matches)
    delay = max(0, int((notification.deliver_after - now).total_seconds()))
    transaction.on_commit(functools.partial(_enqueue_delivery, notification.pk, delay=delay))
    return notification, True


# --- instant alerts ----------------------------------------------------------------------------


def request_instant(match: Match) -> list[Notification]:
    """After scoring (spec §10.4): queue an instant alert if the user's mode and threshold say so.
    Quiet hours push the send to when they end. Never raises for an ineligible match."""
    user = match.user
    row = _settings(user)
    if row.notify_mode != UserSettings.NotifyMode.INSTANT:
        return []
    if match.score is None or match.score < row.instant_score_threshold:
        return []
    if match.status != Match.Status.NEW or not match.rule_pass:
        return []
    now = timezone.now()
    deliver_after = schedule.quiet_hours_end(
        now, row.timezone, row.quiet_hours_start, row.quiet_hours_end
    )
    created: list[Notification] = []
    for channel in channels_for(user, row):
        notification, was_created = create_notification(
            user,
            kind=Notification.Kind.INSTANT_MATCH,
            channel=channel,
            dedupe_key=f"instant:{match.pk}:{channel}",
            payload={"match_id": match.pk},
            deliver_after=deliver_after,
            matches=[match],
        )
        if was_created:
            created.append(notification)
    return created


# --- digests -----------------------------------------------------------------------------------


def digest_matches(user: User) -> tuple[list[Match], int]:
    """Up to 15 new, rule-passed matches (score 40 or more, or not scored yet), best first, plus
    how many more there are."""
    # A match that already has an instant alert waiting or sent is not repeated in a digest.
    alerted = Notification.matches.through.objects.filter(
        match_id=OuterRef("pk"),
        notification__kind=Notification.Kind.INSTANT_MATCH,
        notification__status__in=[Notification.Status.PENDING, Notification.Status.SENT],
    )
    queryset = (
        Match.objects.for_user(user)
        .filter(status=Match.Status.NEW, rule_pass=True)
        .exclude(Exists(alerted))
        .filter(Q(score__gte=DIGEST_MIN_SCORE) | Q(score__isnull=True))
        .select_related("job", "job__source", "user")
        .order_by(F("score").desc(nulls_last=True), "-job__first_seen_at", "-id")
    )
    total = queryset.count()
    items = list(queryset[: render.MAX_DIGEST_ITEMS])
    return items, total - len(items)


def send_digest(
    user: User,
    period_key: str,
    *,
    channels: list[str] | None = None,
    now: datetime.datetime | None = None,
) -> list[Notification]:
    """Create this period's digest for each channel. If there is nothing to say the period is
    simply used up, so a daily user is not asked again until tomorrow."""
    now = now or timezone.now()
    row = _settings(user)
    chosen = channels if channels is not None else channels_for(user, row)
    if not chosen:
        return []
    matches, more = digest_matches(user)
    if not period_key.startswith("manual:"):
        UserSettings.objects.filter(pk=row.pk).update(last_digest_sent_at=now)
    if not matches:
        return []
    created: list[Notification] = []
    for channel in chosen:
        notification, was_created = create_notification(
            user,
            kind=Notification.Kind.DIGEST,
            channel=channel,
            dedupe_key=f"digest:{period_key}:{channel}",
            payload={"period": period_key, "more": more},
            matches=matches,
        )
        if was_created:
            created.append(notification)
    return created


def send_digest_now(user: User, *, now: datetime.datetime | None = None) -> list[Notification]:
    """`/digest`: a digest right now, on the channels the user has, without using up the day's
    scheduled one. Pressing it twice in one minute sends it once."""
    now = now or timezone.now()
    minute = int(now.timestamp() // 60)
    return send_digest(user, f"manual:{minute}", now=now)


def send_test_alert(user: User, *, now: datetime.datetime | None = None) -> str:
    """A real alert for the user's most recent match on Telegram, to check the connection. It
    ignores mode, score and quiet hours, and does not change the match. Returns the outcome."""
    now = now or timezone.now()
    link = active_link(user)
    if link is None or get_gateway() is None:
        return "no_telegram"
    match = Match.objects.for_user(user).order_by("-id").first()
    if match is None:
        return "no_match"
    notification, _ = create_notification(
        user,
        kind=Notification.Kind.INSTANT_MATCH,
        channel=TG,
        dedupe_key=f"test:{match.pk}:{int(now.timestamp())}",
        payload={"match_id": match.pk},
        deliver_after=now,
        matches=[match],
    )
    return deliver(notification.pk, now=now)


# --- the dispatcher ----------------------------------------------------------------------------


def dispatch(now: datetime.datetime | None = None) -> dict[str, int]:
    """Every 5 minutes: send what is due. First notifications whose time has come (deferred by
    quiet hours, waiting for a kit, or a missed task), then each user's due digest."""
    now = now or timezone.now()
    counts = {"due": 0, "digests": 0, "sent": 0}
    stale_claim = now - CLAIM_TIMEOUT
    due_ids = list(
        Notification.objects.filter(status=Notification.Status.PENDING, deliver_after__lte=now)
        .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_claim))
        .values_list("pk", flat=True)[:500]
    )
    for notification_id in due_ids:
        counts["due"] += 1
        if deliver(notification_id, now=now) == SENT:
            counts["sent"] += 1

    rows = (
        UserSettings.objects.exclude(notify_mode=UserSettings.NotifyMode.OFF)
        .filter(user__is_active=True)
        .select_related("user")
    )
    for row in rows:
        period = schedule.digest_period(
            row.notify_mode, now, row.timezone, row.digest_time,
            row.last_digest_sent_at, row.quiet_hours_start, row.quiet_hours_end,
        )  # fmt: skip
        if period is None:
            continue
        counts["digests"] += 1
        for notification in send_digest(row.user, period, now=now):
            if deliver(notification.pk, now=now) == SENT:
                counts["sent"] += 1
    return counts


# --- delivery ----------------------------------------------------------------------------------


def deliver(notification_id: int, *, now: datetime.datetime | None = None) -> str:
    """Send one notification. Safe to call any number of times, from any number of workers."""
    now = now or timezone.now()
    claimed = (
        Notification.objects.filter(
            pk=notification_id, status=Notification.Status.PENDING, deliver_after__lte=now
        )
        .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - CLAIM_TIMEOUT))
        .update(claimed_at=now)
    )
    if not claimed:
        found = Notification.objects.filter(pk=notification_id).first()
        if found is None:
            return GONE
        if found.status != Notification.Status.PENDING:
            return found.status
        return NOT_DUE if found.deliver_after > now else BUSY
    notification = Notification.objects.select_related("user").get(pk=notification_id)
    try:
        outcome = _send(notification, now)
    except GatewayError as exc:
        return _record_failure(notification, exc.reason, retry=not exc.blocked, blocked=exc.blocked)
    except (OSError, smtplib.SMTPException) as exc:
        return _record_failure(notification, type(exc).__name__, retry=True)
    except Exception:
        log_event(
            logger, logging.ERROR, "notification_error", "Notification could not be built",
            notification_id=notification.pk, kind=notification.kind, exc_info=True,
        )  # fmt: skip
        return _record_failure(notification, "internal_error", retry=False)

    if outcome == RETRY:  # waiting for something (a kit); look again shortly
        wait = min(KIT_POLL_SECONDS, 60)
        Notification.objects.filter(pk=notification.pk).update(
            claimed_at=None, deliver_after=now + datetime.timedelta(seconds=wait)
        )
        transaction.on_commit(functools.partial(_enqueue_delivery, notification.pk, delay=wait))
        return RETRY
    status = Notification.Status.SENT if outcome == SENT else Notification.Status.SKIPPED
    Notification.objects.filter(pk=notification.pk).update(
        status=status, sent_at=now if outcome == SENT else None, claimed_at=None, error=""
    )
    log_event(
        logger, logging.INFO, "notification_sent" if outcome == SENT else "notification_skipped",
        "Notification sent" if outcome == SENT else "Notification skipped",
        notification_id=notification.pk, kind=notification.kind, channel=notification.channel,
    )  # fmt: skip
    return outcome


def _record_failure(
    notification: Notification, reason: str, *, retry: bool, blocked: bool = False
) -> str:
    attempts = notification.attempts + 1
    if blocked:
        TelegramLink.objects.filter(user=notification.user).update(active=False)
        UserSettings.objects.filter(user=notification.user).update(notify_telegram=False)
    will_retry = retry and attempts < MAX_ATTEMPTS
    update: dict[str, Any] = {"attempts": attempts, "claimed_at": None, "error": reason[:200]}
    if will_retry:
        delay = 60 * attempts
        update["deliver_after"] = timezone.now() + datetime.timedelta(seconds=delay)
    else:
        update["status"] = Notification.Status.FAILED
    Notification.objects.filter(pk=notification.pk).update(**update)
    log_event(
        logger, logging.WARNING if will_retry else logging.ERROR,
        "notification_failed", "Notification failed",
        notification_id=notification.pk, kind=notification.kind, channel=notification.channel,
        attempt=attempts, reason=reason, will_retry=will_retry, blocked=blocked,
    )  # fmt: skip
    if will_retry:
        transaction.on_commit(
            functools.partial(_enqueue_delivery, notification.pk, delay=60 * attempts)
        )
        return RETRY
    return FAILED


def _send(notification: Notification, now: datetime.datetime) -> str:
    kind = notification.kind
    if kind == Notification.Kind.INSTANT_MATCH:
        return _send_instant(notification, now)
    if kind == Notification.Kind.DIGEST:
        return _send_digest(notification)
    if kind == Notification.Kind.KIT_READY:
        return _send_kit_ready(notification)
    if kind == Notification.Kind.FOLLOWUP:
        return _send_followup(notification)
    return _send_text(notification)


def _chat_and_gateway(notification: Notification) -> tuple[str, TelegramGateway] | None:
    link = active_link(notification.user)
    gateway = get_gateway()
    if link is None or gateway is None:
        return None
    return link.chat_id, gateway


def _send_email(user: User, subject: str, text: str, html: str) -> None:
    message = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [user.email])
    message.attach_alternative(html, "text/html")
    message.send()


def _mark_notified(matches: list[Match], now: datetime.datetime) -> None:
    Match.objects.filter(pk__in=[m.pk for m in matches], status=Match.Status.NEW).update(
        status=Match.Status.NOTIFIED, notified_at=now
    )


def _country(user: User) -> str:
    return _settings(user).country or "KE"


def _send_instant(notification: Notification, now: datetime.datetime) -> str:
    match = (
        Match.objects.for_user(notification.user)
        .select_related("job", "job__source", "user")
        .filter(pk=notification.payload.get("match_id"))
        .first()
    )
    if match is None or match.status in (Match.Status.DISMISSED, Match.Status.APPLIED):
        return SKIPPED
    kits = kit_services.kit_versions(notification.user, match.job)
    kit_services.expire_stuck(kits)
    building = any(k.in_progress for k in kits)
    if building and now < notification.created_at + KIT_WAIT:
        return RETRY  # spec §10.4: wait up to 90 seconds for the kit
    kit = kit_services.latest_ready_kit(notification.user, match.job)
    country = _country(notification.user)

    if notification.channel == TG:
        target = _chat_and_gateway(notification)
        if target is None:
            return SKIPPED
        chat_id, gateway = target
        gateway.send_message(
            chat_id,
            render.instant_text(match, country, kit_ready=kit is not None),
            buttons=render.instant_buttons(match, kit_ready=kit is not None),
        )
        if kit is not None and _settings(notification.user).send_documents_telegram:
            _send_documents(gateway, chat_id, kit)
    else:
        content = render.DigestContent([match], 0, render.site_url("/app/"))
        text, html = render.render_email("digest", render.email_context(content))
        score = "" if match.score is None else f"{match.score} · "
        _send_email(
            notification.user,
            f"New match: {score}{match.job.title} — {match.job.company}",
            text,
            html,
        )
    _mark_notified([match], now)
    return SENT


def _send_digest(notification: Notification) -> str:
    matches = list(
        notification.matches.select_related("job", "job__source", "user")
        .exclude(status__in=[Match.Status.DISMISSED, Match.Status.APPLIED])
        .order_by(F("score").desc(nulls_last=True), "-job__first_seen_at", "-id")
    )
    if not matches:
        return SKIPPED
    content = render.DigestContent(
        matches, int(notification.payload.get("more", 0)), render.site_url("/app/")
    )
    if notification.channel == TG:
        target = _chat_and_gateway(notification)
        if target is None:
            return SKIPPED
        target[1].send_message(target[0], render.digest_text(content))
    else:
        text, html = render.render_email("digest", render.email_context(content))
        count = len(matches) + content.more
        plural = "es" if count != 1 else ""
        _send_email(
            notification.user, f"{count} new match{plural} on {settings.SITE_NAME}", text, html
        )
    _mark_notified(matches, timezone.now())
    return SENT


# --- kits ----------------------------------------------------------------------------------------


def _send_documents(gateway: TelegramGateway, chat_id: str, kit: Kit) -> int:
    """The CV and cover letter PDFs as Telegram documents. Returns how many were sent."""
    sent = 0
    for key in ("cv_pdf", "cover_letter_pdf"):
        field = getattr(kit, key)
        if not field:
            continue
        try:
            with field.open("rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            continue
        gateway.send_document(chat_id, kit_documents.download_filename(kit, key), data)
        sent += 1
    return sent


def _send_kit_ready(notification: Notification) -> str:
    kit = (
        Kit.objects.for_user(notification.user)
        .select_related("job", "user")
        .filter(pk=notification.payload.get("kit_id"))
        .first()
    )
    if kit is None:
        return SKIPPED
    kit_services.expire_stuck([kit])
    kit.refresh_from_db()
    url = render.site_url(f"/app/jobs/{kit.job_id}/#kit")
    if kit.in_progress:
        return (
            RETRY
            if notification.created_at + datetime.timedelta(minutes=15) > timezone.now()
            else SKIPPED
        )
    if kit.status == Kit.Status.FAILED:
        if notification.channel != TG:
            return SKIPPED
        target = _chat_and_gateway(notification)
        if target is None:
            return SKIPPED
        target[1].send_message(
            target[0], f"The kit for {kit.job.title} could not be prepared. Try again here: {url}"
        )
        return SENT
    if notification.channel == TG:
        target = _chat_and_gateway(notification)
        if target is None:
            return SKIPPED
        chat_id, gateway = target
        gateway.send_message(chat_id, render.kit_ready_text(kit.job.title, kit.job.company, url))
        if _settings(notification.user).send_documents_telegram:
            _send_documents(gateway, chat_id, kit)
    else:
        context = {"job_title": kit.job.title, "company": kit.job.company, "url": url,
                   "site_name": settings.SITE_NAME}  # fmt: skip
        text, html = render.render_email("kit_ready", context)
        _send_email(
            notification.user, f"Your application kit for {kit.job.title} is ready", text, html
        )
    return SENT


def _kit_ready_rows(user: User, kit: Kit, only: list[str] | None = None) -> list[Notification]:
    rows = []
    for channel in channels_for(user):
        if only is not None and channel not in only:
            continue
        notification, _ = create_notification(
            user,
            kind=Notification.Kind.KIT_READY,
            channel=channel,
            dedupe_key=f"kit_ready:{kit.pk}:{channel}",
            payload={"kit_id": kit.pk},
        )
        rows.append(notification)
    return rows


def kit_for_button(user: User, match: Match) -> str:
    """The Telegram "Kit" button. Returns what to tell the user: `ready` (documents sent),
    `queued` (a kit is on its way and will be sent when done) or `limit` / `empty`."""
    from apps.kits.exceptions import EmptyProfile, KitError
    from apps.llm.exceptions import QuotaExceeded

    ready = kit_services.latest_ready_kit(user, match.job)
    link = active_link(user)
    gateway = get_gateway()
    if ready is not None:
        if link and gateway and not _send_documents(gateway, link.chat_id, ready):
            gateway.send_message(
                link.chat_id,
                render.kit_ready_text(
                    match.job.title, match.job.company,
                    render.site_url(f"/app/jobs/{match.job_id}/#kit"),
                ),
            )  # fmt: skip
        return "ready"
    try:
        kit = kit_services.request_kit(user, match.job, match=match)
    except QuotaExceeded:
        return "limit"
    except EmptyProfile:
        return "empty"
    except KitError:
        return "limit"
    _kit_ready_rows(user, kit, only=[TG])  # they asked on Telegram, so the answer goes there
    return "queued"


def on_kit_ready(kit: Kit) -> None:
    """Called when a kit finishes (`kits.hooks.after_generate`): send the kit-ready messages that
    were waiting for it. A kit that finished after its instant alert went out also gets one."""
    user = kit.user
    waiting = Notification.objects.filter(
        user=user, kind=Notification.Kind.KIT_READY, status=Notification.Status.PENDING,
        payload__kit_id=kit.pk,
    )  # fmt: skip
    ids = list(waiting.values_list("pk", flat=True))
    if not ids and kit.auto_generated and kit.match_id:
        late = Notification.objects.filter(
            user=user, kind=Notification.Kind.INSTANT_MATCH, status=Notification.Status.SENT,
            payload__match_id=kit.match_id,
        ).exists()  # fmt: skip
        if late:
            ids = [n.pk for n in _kit_ready_rows(user, kit) if n.channel == TG]
    for notification_id in ids:
        Notification.objects.filter(pk=notification_id).update(deliver_after=timezone.now())
        deliver(notification_id)


# --- follow-up reminders (spec §13.5) ----------------------------------------


def _send_followup(notification: Notification) -> str:
    """A follow-up reminder or a "consider marking it ghosted" suggestion, built from the
    application's ID. Skipped if the application has moved on since it was queued."""
    from apps.tracker.models import Application, Stage

    application = (
        Application.objects.for_user(notification.user)
        .select_related("job")
        .filter(pk=notification.payload.get("application_id"))
        .first()
    )
    if application is None or application.stage != Stage.APPLIED:
        return SKIPPED
    ghost = notification.payload.get("what") == "ghost"
    url = render.site_url(f"/app/tracker/{application.pk}/")
    job = application.job
    if ghost:
        text = (
            f"👻 No reply from {job.company} for 3 weeks about {job.title}. "
            f"Consider marking it as ghosted: {url}"
        )
        subject = f"No reply yet from {job.company}"
    else:
        days = (timezone.now() - (application.applied_at or application.created_at)).days
        text = (
            f"⏰ Time to follow up on {job.title} at {job.company}. "
            f"You applied {days} day{'s' if days != 1 else ''} ago. {url}"
        )
        subject = f"Follow up on {job.title} at {job.company}"
    if notification.channel == TG:
        target = _chat_and_gateway(notification)
        if target is None:
            return SKIPPED
        target[1].send_message(target[0], text)
    else:
        context = {
            "message": text.split(url)[0].strip(),
            "url": url,
            "site_name": settings.SITE_NAME,
        }
        body, html = render.render_email("followup", context)
        _send_email(notification.user, subject, body, html)
    return SENT


# --- text-only and staff notices ----------------------------------------------------------------


def _send_text(notification: Notification) -> str:
    """Notices built from IDs in the payload: source health and new waitlist requests."""
    from apps.accounts.models import WaitlistEntry
    from apps.sources.models import Source

    payload = notification.payload
    if payload.get("source_id"):
        source = Source.objects.filter(pk=payload["source_id"]).first()
        if source is None:
            return SKIPPED
        text = (
            f"⚠️ Source {source.display_name} is {source.health} "
            f"({source.consecutive_failures} failures in a row)."
        )
    elif payload.get("waitlist_entry_id"):
        entry = WaitlistEntry.objects.filter(pk=payload["waitlist_entry_id"]).first()
        if entry is None:
            return SKIPPED
        text = (
            f"📝 New waitlist request from {entry.name or 'someone'}. Review it: "
            + render.site_url(f"/admin/accounts/waitlistentry/{entry.pk}/change/")
        )
    elif payload.get("forwarding_confirmation_id"):
        text = (
            "📬 Gmail is asking you to confirm forwarding to JobPilot. "
            "Open your job alert settings and press the confirmation link: "
            + render.site_url("/app/settings/job-alerts/")
        )
    elif payload.get("followup_text"):
        text = str(payload["followup_text"])
    else:
        return SKIPPED
    target = _chat_and_gateway(notification)
    if target is None:
        return SKIPPED
    target[1].send_message(target[0], text)
    return SENT


def notify_staff(kind: str, dedupe_key: str, payload: dict[str, Any]) -> int:
    """Tell every staff user who has linked Telegram. Returns how many were queued."""
    if get_gateway() is None:
        return 0
    queued = 0
    staff = User.objects.filter(is_staff=True, is_active=True, telegram_link__active=True)
    for user in staff:
        _, created = create_notification(
            user, kind=kind, channel=TG, dedupe_key=dedupe_key, payload=payload
        )
        queued += int(created)
    return queued


def notify_source_health(source_id: int, health: str, when: datetime.date) -> int:
    key = f"source_health:{source_id}:{health}:{when.isoformat()}"
    return notify_staff(Notification.Kind.SOURCE_HEALTH, key, {"source_id": source_id})


def notify_new_waitlist_entry(entry_id: int) -> int:
    return notify_staff(
        Notification.Kind.SYSTEM, f"waitlist:{entry_id}", {"waitlist_entry_id": entry_id}
    )
