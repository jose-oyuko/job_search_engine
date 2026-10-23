"""Shared setup for matching tests."""

from typing import Any

from django.utils import timezone

from apps.accounts.models import UserSettings
from apps.jobs.models import Job
from apps.profiles import services as profile_services
from apps.profiles.models import Track
from tests.factories import JobFactory, SkillFactory, TrackFactory, UserFactory


def make_user(
    *,
    country: str = "KE",
    skills: tuple[str, ...] = ("Helpdesk", "Excel"),
    headline: str = "Support specialist",
    track: bool = True,
    **track_fields: Any,
) -> Any:
    """A user the matcher can work with: settings, a profile with some content and a track."""
    user = UserFactory()
    UserSettings.objects.create(user=user, country=country, onboarding_completed=True)
    profile = profile_services.get_profile(user)
    profile.headline = headline
    profile.save()
    for name in skills:
        SkillFactory(user=user, name=name)
    if track:
        defaults: dict[str, Any] = {
            "is_default": True,
            "name": "Support",
            "target_titles": ["support"],
            "work_types": ["remote"],
        }
        TrackFactory(user=user, **{**defaults, **track_fields})
    return user


def make_job(**fields: Any) -> Job:
    """A public, active, eligible-for-everyone remote support job unless told otherwise."""
    defaults: dict[str, Any] = {
        "title": "Customer Support Agent",
        "company": "Acme Ltd",
        "description_text": "Answer customers.",
        "work_type": "remote",
        "is_global": True,
        "eligibility_known": True,
        "first_seen_at": timezone.now(),
    }
    return JobFactory(**{**defaults, **fields})


def default_track(user: Any) -> Track:
    return Track.objects.for_user(user).get(is_default=True)
