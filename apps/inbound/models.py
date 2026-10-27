"""Forwarding addresses, the emails received and Gmail forwarding confirmations (spec §5.10)."""

import secrets

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel, UserOwnedModel

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (0/o, 1/l/i)
TOKEN_LENGTH = 6


def new_token() -> str:
    """`u-` plus six characters, for example `u-7f3k9a`."""
    return "u-" + "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_LENGTH))


class InboundAddress(UserOwnedModel, TimeStampedModel):
    token = models.CharField(max_length=20, unique=True, default=new_token)
    active = models.BooleanField(default=True)

    @property
    def address(self) -> str:
        return f"{self.token}@{settings.INBOUND_EMAIL_DOMAIN}"

    def __str__(self) -> str:
        return f"Inbound address of user {self.user_id}"


class InboundEmail(UserOwnedModel, TimeStampedModel):
    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PARSED = "parsed", "Read"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    address = models.ForeignKey(InboundAddress, on_delete=models.CASCADE, related_name="emails")
    message_id = models.CharField(max_length=300, unique=True)
    from_address = models.CharField(max_length=254, blank=True)
    subject = models.CharField(max_length=300, blank=True)
    received_at = models.DateTimeField()
    # Untrusted content: cleared after 30 days (`services.clear_old_bodies`), metadata stays.
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    parser = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED)
    jobs_found = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        indexes = [models.Index(fields=["user", "-received_at"])]

    def __str__(self) -> str:
        return f"Inbound email #{self.pk}"


class ForwardingConfirmation(UserOwnedModel, TimeStampedModel):
    provider = models.CharField(max_length=20, default="gmail")
    confirmation_url = models.URLField(max_length=1000)
    confirmation_code = models.CharField(max_length=20, blank=True)
    received_at = models.DateTimeField()
    dismissed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:
        return f"Forwarding confirmation #{self.pk}"
