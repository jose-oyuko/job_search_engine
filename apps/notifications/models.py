"""Telegram links, link tokens and the notification log (spec §5.9)."""

import datetime
import secrets

from django.db import models
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import TimeStampedModel, UserOwnedModel
from apps.matching.models import Match

LINK_TOKEN_MINUTES = 15


class TelegramLink(TimeStampedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="telegram_link")
    # Telegram chat IDs are personal data: never logged, only hashed (`signing.chat_hash`).
    chat_id = models.CharField(max_length=32, unique=True)
    username = models.CharField(max_length=64, blank=True)
    linked_at = models.DateTimeField(default=timezone.now)
    active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"Telegram link of user {self.user_id}"


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _default_expiry() -> datetime.datetime:
    return timezone.now() + datetime.timedelta(minutes=LINK_TOKEN_MINUTES)


class TelegramLinkToken(UserOwnedModel, TimeStampedModel):
    token = models.CharField(max_length=64, unique=True, default=_new_token)
    expires_at = models.DateTimeField(default=_default_expiry)
    used_at = models.DateTimeField(null=True, blank=True)

    def is_usable(self, now: datetime.datetime | None = None) -> bool:
        return self.used_at is None and self.expires_at > (now or timezone.now())

    def __str__(self) -> str:
        return f"Link token #{self.pk}"


class Notification(UserOwnedModel, TimeStampedModel):
    class Kind(models.TextChoices):
        INSTANT_MATCH = "instant_match", "Instant match"
        DIGEST = "digest", "Digest"
        KIT_READY = "kit_ready", "Kit ready"
        FOLLOWUP = "followup_reminder", "Follow-up reminder"
        SOURCE_HEALTH = "source_health", "Source health"
        SYSTEM = "system", "System"

    class Channel(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        EMAIL = "email", "Email"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    # What to send: ids and small facts only. The text is rendered from live data at send time.
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    # Not sent before this time (quiet hours, waiting for a kit).
    deliver_after = models.DateTimeField(default=timezone.now)
    # Set while a worker is sending, so two workers never send the same row.
    claimed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)
    dedupe_key = models.CharField(max_length=120)
    matches = models.ManyToManyField(Match, blank=True, related_name="notifications")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "dedupe_key"], name="notification_unique_key"),
        ]
        indexes = [models.Index(fields=["status", "deliver_after"])]

    def __str__(self) -> str:
        return f"Notification #{self.pk} ({self.kind}/{self.channel})"
