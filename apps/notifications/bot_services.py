"""What the Telegram bot does (spec §13.1). The python-telegram-bot glue in `bot.py` only hands
updates to these functions; they use the `TelegramGateway`, so tests run them with a fake."""

import logging
import re

from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import User, UserSettings
from apps.core.logging import log_event
from apps.core.tasks import enqueue
from apps.matching import services as matching_services
from apps.matching.exceptions import SearchTooSoon
from apps.matching.models import Match
from apps.matching.tasks import search_now_task
from apps.tracker import services as tracker_services

from . import schedule, services, signing
from .exceptions import ChatAlreadyLinked, LinkTokenInvalid
from .gateway import TelegramGateway, get_gateway

logger = logging.getLogger(__name__)

RATE_LIMIT = 30  # commands per chat per minute (spec §20)
INVITE_ONLY = (
    "This bot is invite-only. If you have a JobPilot account, open Settings → Notifications "
    "on the website and press Connect Telegram."
)
HELP = (
    "/status: new matches today\n"
    "/search: look for new jobs now\n"
    "/digest: send me a digest now\n"
    "/pause: stop alerts\n"
    "/resume: start alerts again\n"
    "/help: this list"
)
_COMMAND_RE = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)


def _allowed(chat_id: str) -> bool:
    """30 commands a minute per chat. Over the limit the bot goes quiet rather than reply."""
    key = f"tg_rate:{signing.chat_hash(chat_id)}"
    cache.add(key, 0, timeout=60)
    try:
        return int(cache.incr(key)) <= RATE_LIMIT
    except ValueError:  # the key expired between add and incr
        cache.set(key, 1, timeout=60)
        return True


def handle_text(chat_id: str, username: str, text: str) -> None:
    """One incoming message. Anything that is not a command gets the help text (or the
    invite-only notice for a chat that is not linked)."""
    gateway = get_gateway()
    if gateway is None or not _allowed(chat_id):
        return
    user = services.user_for_chat(chat_id)
    found = _COMMAND_RE.match(text.strip())
    command = found.group(1).lower() if found else ""
    argument = (found.group(2) or "").strip() if found else ""

    if command == "start":
        _start(gateway, chat_id, username, argument, user)
        return
    if user is None:
        gateway.send_message(chat_id, INVITE_ONLY)
        return
    handlers = {
        "status": _status,
        "search": _search,
        "digest": _digest,
        "pause": _pause,
        "resume": _resume,
        "help": _help,
    }
    handler = handlers.get(command)
    log_event(
        logger, logging.DEBUG, "bot_command", "Bot command",
        chat=signing.chat_hash(chat_id), command=command if handler else "unknown",
    )  # fmt: skip
    if handler is None:
        gateway.send_message(chat_id, "I did not understand that.\n\n" + HELP)
        return
    handler(gateway, chat_id, user)


def _help(gateway: TelegramGateway, chat_id: str, user: User) -> None:
    gateway.send_message(chat_id, HELP)


def _start(
    gateway: TelegramGateway, chat_id: str, username: str, token: str, user: User | None
) -> None:
    if token:
        try:
            link = services.consume_link_token(token, chat_id, username)
        except (LinkTokenInvalid, ChatAlreadyLinked) as exc:
            gateway.send_message(chat_id, exc.message)
            return
        gateway.send_message(
            chat_id,
            f"Connected to JobPilot as {link.user.email}. "
            f"You will get your job alerts here.\n\n{HELP}",
        )
        return
    if user is not None:
        gateway.send_message(chat_id, f"You are connected.\n\n{HELP}")
    else:
        gateway.send_message(chat_id, INVITE_ONLY)


def _status(gateway: TelegramGateway, chat_id: str, user: User) -> None:
    row, _ = UserSettings.objects.get_or_create(user=user)
    local_midnight = (
        timezone.now()
        .astimezone(schedule.zone(row.timezone))
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    matches = Match.objects.for_user(user).filter(rule_pass=True)
    today = matches.filter(created_at__gte=local_midnight)
    waiting = matches.filter(status__in=[Match.Status.NEW, Match.Status.NOTIFIED]).count()
    best = (
        today.exclude(score__isnull=True).order_by("-score").values_list("score", flat=True).first()
    )
    lines = [f"New matches today: {today.count()}", f"Not looked at yet: {waiting}"]
    if best is not None:
        lines.append(f"Best score today: {best}")
    lines.append(f"Alerts: {row.get_notify_mode_display().lower()}")
    counts = {k: v for k, v in tracker_services.stage_counts(user).items() if v}
    if counts:
        lines.append("Applications: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    gateway.send_message(chat_id, "\n".join(lines))


def _search(gateway: TelegramGateway, chat_id: str, user: User) -> None:
    try:
        matching_services.start_search(user)
    except SearchTooSoon as exc:
        minutes = max(1, round(exc.wait.total_seconds() / 60))
        gateway.send_message(
            chat_id, f"You searched a moment ago. Try again in about {minutes} min."
        )
        return
    enqueue(search_now_task, user.pk)
    gateway.send_message(chat_id, "Looking for new jobs. I will tell you when there is something.")


def _digest(gateway: TelegramGateway, chat_id: str, user: User) -> None:
    created = services.send_digest_now(user)
    if not created:
        gateway.send_message(chat_id, "Nothing new to report right now.")
        return
    for notification in created:
        services.deliver(notification.pk)


def _pause(gateway: TelegramGateway, chat_id: str, user: User) -> None:
    row, _ = UserSettings.objects.get_or_create(user=user)
    if row.notify_mode == UserSettings.NotifyMode.OFF:
        gateway.send_message(chat_id, "Alerts are already paused. /resume turns them back on.")
        return
    row.previous_notify_mode = row.notify_mode
    row.notify_mode = UserSettings.NotifyMode.OFF
    row.save(update_fields=["previous_notify_mode", "notify_mode", "updated_at"])
    gateway.send_message(
        chat_id, "Alerts paused. Your dashboard keeps updating. /resume to restart."
    )


def _resume(gateway: TelegramGateway, chat_id: str, user: User) -> None:
    row, _ = UserSettings.objects.get_or_create(user=user)
    if row.notify_mode != UserSettings.NotifyMode.OFF:
        gateway.send_message(chat_id, "Alerts are already on.")
        return
    valid = {m.value for m in UserSettings.NotifyMode} - {UserSettings.NotifyMode.OFF}
    row.notify_mode = row.previous_notify_mode if row.previous_notify_mode in valid else "instant"
    row.previous_notify_mode = ""
    row.save(update_fields=["notify_mode", "previous_notify_mode", "updated_at"])
    gateway.send_message(chat_id, f"Alerts are back on ({row.get_notify_mode_display().lower()}).")


def handle_callback(chat_id: str, callback_id: str, data: str) -> None:
    """A button press. The chat must belong to the user the button was made for."""
    gateway = get_gateway()
    if gateway is None or not _allowed(chat_id):
        return
    user = services.user_for_chat(chat_id)
    parsed = signing.verify_callback(data, user.pk) if user is not None else None
    if user is None or parsed is None:
        log_event(
            logger, logging.WARNING, "bot_callback_rejected", "Bot callback rejected",
            chat=signing.chat_hash(chat_id),
        )  # fmt: skip
        gateway.answer_callback(callback_id, "This button is no longer valid.")
        return
    action, match_id = parsed
    match = (
        Match.objects.for_user(user)
        .select_related("job", "job__source", "user")
        .filter(pk=match_id)
        .first()
    )
    if match is None:
        gateway.answer_callback(callback_id, "This job is no longer available.")
        return

    if action == "applied":
        tracker_services.apply_to_job(user, match.job, match=match)
        gateway.answer_callback(callback_id, "Marked as applied ✓")
    elif action == "dismiss":
        matching_services.set_status(
            user, match, Match.Status.DISMISSED, reason=Match.DismissReason.NOT_RELEVANT
        )
        gateway.answer_callback(callback_id, "Dismissed. It will not show on your dashboard.")
    else:
        outcome = services.kit_for_button(user, match)
        answers = {
            "ready": "Sending your CV and cover letter…",
            "queued": "Preparing your kit. I will send it here when it is ready.",
            "limit": "You have used this month's application kits.",
            "empty": "Add your experience or skills to your profile first.",
        }
        gateway.answer_callback(callback_id, answers[outcome])
    log_event(
        logger, logging.INFO, "bot_callback", "Bot callback handled",
        for_user_id=user.pk, action=action, match_id=match.pk,
    )  # fmt: skip
