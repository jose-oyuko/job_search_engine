"""Accounts: the custom User, per-user settings, invites, the waitlist and consent records."""

import datetime
import secrets
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel, UserOwnedModel, UserOwnedQuerySet


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: Any) -> "User":
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> "User":
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> "User":
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Email is the login identifier; there is no username."""

    username = None  # type: ignore[assignment]
    email = models.EmailField("email address", unique=True)
    full_name = models.CharField(max_length=150, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # noqa: RUF012

    objects = UserManager()  # type: ignore[assignment,misc]

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Emails are case-insensitive in practice; store them lowercased so uniqueness holds.
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.email


class UserSettings(TimeStampedModel):
    """One row per user, created at signup (see `services.complete_signup`)."""

    class NotifyMode(models.TextChoices):
        INSTANT = "instant", "Instant"
        HOURLY = "hourly", "Hourly digest"
        DAILY = "daily", "Daily digest"
        OFF = "off", "Off"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="settings")
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")
    country = models.CharField(max_length=2, default="KE", help_text="ISO 3166-1 alpha-2 code.")
    notify_mode = models.CharField(
        max_length=10, choices=NotifyMode.choices, default=NotifyMode.INSTANT
    )
    digest_time = models.TimeField(default=datetime.time(8, 0), help_text="Local time.")
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    instant_score_threshold = models.PositiveSmallIntegerField(default=75)
    auto_kit_threshold = models.PositiveSmallIntegerField(default=85)
    notify_telegram = models.BooleanField(default=False)
    notify_email = models.BooleanField(default=True)
    # Send the CV and cover letter PDFs as Telegram documents when a kit is ready (spec §13.1).
    send_documents_telegram = models.BooleanField(default=True)
    # What `/resume` restores after `/pause` (which sets `notify_mode` to off).
    previous_notify_mode = models.CharField(max_length=10, blank=True)
    last_digest_sent_at = models.DateTimeField(null=True, blank=True)
    last_manual_search_at = models.DateTimeField(null=True, blank=True)
    share_anonymous_outcomes = models.BooleanField(default=False)
    onboarding_completed = models.BooleanField(default=False)

    objects = UserOwnedQuerySet.as_manager()

    class Meta:
        verbose_name_plural = "user settings"

    def __str__(self) -> str:
        return f"Settings of user {self.user_id}"


def _new_invite_code() -> str:
    return secrets.token_urlsafe(16)


def _default_invite_expiry() -> datetime.datetime:
    return timezone.now() + datetime.timedelta(days=settings.INVITE_EXPIRY_DAYS)


class Invite(TimeStampedModel):
    code = models.CharField(max_length=40, unique=True, default=_new_invite_code, editable=False)
    email = models.EmailField(blank=True, help_text="If set, only this email can use the invite.")
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expires_at = models.DateTimeField(default=_default_invite_expiry)
    used_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    used_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def __str__(self) -> str:
        # Never include the code: it is the credential.
        return f"Invite #{self.pk}"


class WaitlistEntry(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        INVITED = "invited", "Invited"
        REJECTED = "rejected", "Rejected"

    email = models.EmailField()
    name = models.CharField(max_length=150)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    invite = models.ForeignKey(
        Invite, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "waitlist entries"

    def __str__(self) -> str:
        return f"Waitlist entry #{self.pk}"


class ConsentRecord(UserOwnedModel):
    class Kind(models.TextChoices):
        TERMS = "terms", "Terms of use"
        PRIVACY = "privacy", "Privacy policy"
        OUTCOME_SHARING = "outcome_sharing", "Anonymous outcome sharing"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    version = models.CharField(max_length=20)
    granted = models.BooleanField(default=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"{self.kind} v{self.version} ({'granted' if self.granted else 'withdrawn'})"
