"""Shared setup for notification tests."""

import datetime
from typing import Any

import pytest

from apps.accounts.models import UserSettings
from apps.notifications.gateway import FakeGateway, get_gateway
from apps.notifications.models import TelegramLink
from tests.factories import MatchFactory
from tests.kit_helpers import make_kit_job, make_kit_user

CHAT = "555001"


@pytest.fixture
def tg() -> FakeGateway:
    """The in-memory Telegram gateway, emptied before each test."""
    gateway = get_gateway()
    assert isinstance(gateway, FakeGateway)
    gateway.reset()
    return gateway


def make_notif_user(
    *, chat_id: str | None = CHAT, full_name: str = "Jane Wanjiru Doe", **settings_fields: Any
) -> Any:
    """A user with a profile, Telegram linked (unless `chat_id` is None) and email on."""
    user, _ = make_kit_user(full_name=full_name)
    defaults: dict[str, Any] = {
        "notify_mode": "instant",
        "notify_email": True,
        "notify_telegram": chat_id is not None,
    }
    UserSettings.objects.filter(user=user).update(**{**defaults, **settings_fields})
    if chat_id is not None:
        TelegramLink.objects.create(user=user, chat_id=chat_id, username="jane")
    return user


def scored_match(user: Any, *, score: int = 90, **fields: Any) -> Any:
    job = fields.pop("job", None) or make_kit_job(external_id=f"j{score}-{id(fields)}")
    defaults: dict[str, Any] = {
        "score": score,
        "verdict": "" if score is None else "strong" if score >= 80 else "good",
        "strengths": ["Helpdesk experience"],
        "gaps": ["No Kubernetes"],
        "scored_profile_version": 1,
        "status": "new",
    }
    return MatchFactory(user=user, job=job, **{**defaults, **fields})


def utc(*args: int) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=datetime.UTC)
