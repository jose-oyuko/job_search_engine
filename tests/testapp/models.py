"""Test-only models, installed by settings/test.py, for exercising abstract base classes."""

from django.db import models

from apps.core.models import TimeStampedModel, UserOwnedModel


class Note(UserOwnedModel, TimeStampedModel):
    text = models.CharField(max_length=100)
