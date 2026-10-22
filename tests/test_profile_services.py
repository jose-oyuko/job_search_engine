"""Profile services: versioning, ordering, ownership, tracks, completeness and the serialiser."""

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.cache import cache
from django.db import IntegrityError, transaction

from apps.profiles import serializers, services
from apps.profiles.exceptions import NotOwner
from apps.profiles.models import (
    Certification,
    Education,
    Experience,
    ExperienceBullet,
    Profile,
    Project,
    Skill,
    Track,
)
from tests.factories import (
    BulletFactory,
    CertificationFactory,
    EducationFactory,
    ExperienceFactory,
    ProjectFactory,
    SkillFactory,
    TrackFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db


def _version(user: Any) -> int:
    return int(services.get_profile(user).version)


# --- versioning -----------------------------------------------------------------------------------


def test_a_new_profile_starts_at_version_one() -> None:
    assert _version(UserFactory()) == 1


def test_every_kind_of_change_bumps_the_version_exactly_once() -> None:
    user = UserFactory()
    start = _version(user)

    exp = Experience(title="T", company="C")
    services.save_item(user, exp)
    assert _version(user) == start + 1

    exp.title = "T2"
    services.save_item(user, exp)
    assert _version(user) == start + 2

    bullet = ExperienceBullet(experience=exp, text="did it")
    services.save_item(user, bullet)
    assert _version(user) == start + 3

    services.move_item(user, bullet, "down")
    assert _version(user) == start + 4

    services.delete_item(user, bullet)
    assert _version(user) == start + 5

    profile = services.get_profile(user)
    profile.headline = "Hi"
    services.save_profile(user, profile)
    assert _version(user) == start + 6

    services.save_track(user, Track(name="Main"))
    assert _version(user) == start + 7


def test_the_version_never_decreases_and_is_per_user() -> None:
    a, b = UserFactory(), UserFactory()
    services.save_item(a, Experience(title="T", company="C"))
    assert _version(a) == 2 and _version(b) == 1


def test_a_failed_save_does_not_bump_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    user = UserFactory()
    before = _version(user)
    monkeypatch.setattr(
        Experience, "save", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError())
    )
    with pytest.raises(RuntimeError):
        services.save_item(user, Experience(title="T", company="C"))
    assert _version(user) == before


# --- item CRUD, ordering ---------------------------------------------------------------------------


def test_new_items_are_appended_in_order() -> None:
    user = UserFactory()
    items = [Experience(title=f"T{i}", company="C") for i in range(3)]
    for item in items:
        services.save_item(user, item)
    assert [e.order for e in Experience.objects.for_user(user)] == [0, 1, 2]


def test_move_swaps_neighbours_and_ignores_the_ends() -> None:
    user = UserFactory()
    a, b, c = (ExperienceFactory(user=user, order=i, title=n) for i, n in enumerate("ABC"))

    services.move_item(user, c, "up")
    assert [e.title for e in Experience.objects.for_user(user)] == ["A", "C", "B"]
    services.move_item(user, c, "up")
    services.move_item(user, c, "up")  # already first: no-op
    assert [e.title for e in Experience.objects.for_user(user)] == ["C", "A", "B"]
    services.move_item(user, b, "down")  # already last: no-op
    assert [e.title for e in Experience.objects.for_user(user)] == ["C", "A", "B"]


def test_move_repairs_duplicate_order_values() -> None:
    user = UserFactory()
    for name in "AB":
        ExperienceFactory(user=user, order=0, title=name)  # ties
    first = Experience.objects.for_user(user).first()
    services.move_item(user, first, "down")
    assert sorted(e.order for e in Experience.objects.for_user(user)) == [0, 1]


def test_bullets_are_ordered_within_their_own_experience() -> None:
    user = UserFactory()
    one, two = ExperienceFactory(user=user), ExperienceFactory(user=user)
    b1, b2 = BulletFactory(experience=one, order=0), BulletFactory(experience=one, order=1)
    other = BulletFactory(experience=two, order=0)
    services.move_item(user, b2, "up")
    assert list(one.bullets.values_list("pk", flat=True)) == [b2.pk, b1.pk]
    assert other.order == ExperienceBullet.objects.get(pk=other.pk).order == 0

    fresh = ExperienceBullet(experience=two, text="new")
    services.save_item(user, fresh)
    assert fresh.order == 1  # next within experience two, not across experiences


def test_move_rejects_a_bad_direction() -> None:
    user = UserFactory()
    exp = ExperienceFactory(user=user)
    with pytest.raises(ValueError):
        services.move_item(user, exp, "sideways")


def test_deleting_an_experience_deletes_its_bullets() -> None:
    user = UserFactory()
    exp = ExperienceFactory(user=user)
    BulletFactory(experience=exp)
    services.delete_item(user, exp)
    assert not ExperienceBullet.objects.exists()


def test_skills_attached_to_items_must_belong_to_the_user() -> None:
    user, other = UserFactory(), UserFactory()
    mine, theirs = SkillFactory(user=user), SkillFactory(user=other)
    exp = ExperienceFactory(user=user)
    bullet = ExperienceBullet(experience=exp, text="x")
    services.save_item(user, bullet, m2m={"skills": [mine, theirs]})
    assert list(bullet.skills.all()) == [mine]


# --- ownership ---------------------------------------------------------------------------------------


def test_services_refuse_to_touch_someone_elses_rows() -> None:
    owner, intruder = UserFactory(), UserFactory()
    exp = ExperienceFactory(user=owner)
    bullet = BulletFactory(experience=exp)
    for action in (
        lambda: services.save_item(intruder, exp),
        lambda: services.delete_item(intruder, exp),
        lambda: services.move_item(intruder, exp, "up"),
        lambda: services.delete_item(intruder, bullet),
        lambda: services.save_profile(intruder, services.get_profile(owner)),
        lambda: services.delete_track(intruder, TrackFactory(user=owner)),
        lambda: services.save_track(intruder, TrackFactory(user=owner)),
    ):
        with pytest.raises(NotOwner):
            action()
    assert Experience.objects.filter(pk=exp.pk).exists()


def test_a_bullet_cannot_be_attached_to_someone_elses_experience() -> None:
    owner, intruder = UserFactory(), UserFactory()
    exp = ExperienceFactory(user=owner)
    with pytest.raises(NotOwner):
        services.save_item(intruder, ExperienceBullet(experience=exp, text="sneaky"))
    assert not ExperienceBullet.objects.exists()


# --- skills --------------------------------------------------------------------------------------------


def test_skill_names_are_unique_per_user_ignoring_case() -> None:
    user, other = UserFactory(), UserFactory()
    SkillFactory(user=user, name="Python")
    with pytest.raises(IntegrityError), transaction.atomic():
        SkillFactory(user=user, name="python")
    SkillFactory(user=other, name="python")  # another user may have it


def test_find_or_create_skill_is_case_insensitive_and_clips() -> None:
    user = UserFactory()
    first, created = services.find_or_create_skill(user, "  Excel ")
    again, created_again = services.find_or_create_skill(user, "EXCEL")
    assert created and not created_again and first == again
    assert len(services.find_or_create_skill(user, "x" * 300)[0].name) == 100


# --- tracks ---------------------------------------------------------------------------------------------


def test_the_first_track_becomes_the_default() -> None:
    user = UserFactory()
    track = services.save_track(user, Track(name="One"))
    assert track.is_default


def test_later_tracks_are_not_default_unless_asked() -> None:
    user = UserFactory()
    first = services.save_track(user, Track(name="One"))
    second = services.save_track(user, Track(name="Two"))
    assert not second.is_default
    third = services.save_track(user, Track(name="Three"), make_default=True)
    first.refresh_from_db()
    second.refresh_from_db()
    assert third.is_default and not first.is_default and not second.is_default
    assert Track.objects.for_user(user).filter(is_default=True).count() == 1


def test_editing_a_track_keeps_its_default_flag() -> None:
    user = UserFactory()
    track = services.save_track(user, Track(name="One"))
    other = services.save_track(user, Track(name="Two"))
    track.name = "Renamed"
    services.save_track(user, track)
    other.name = "Also renamed"
    services.save_track(user, other)
    track.refresh_from_db()
    other.refresh_from_db()
    assert track.is_default and not other.is_default


def test_deleting_the_default_promotes_the_oldest_remaining_track() -> None:
    user = UserFactory()
    first = services.save_track(user, Track(name="One"))
    second = services.save_track(user, Track(name="Two"))
    third = services.save_track(user, Track(name="Three"))
    services.delete_track(user, first)
    second.refresh_from_db()
    third.refresh_from_db()
    assert second.is_default and not third.is_default


def test_deleting_a_non_default_track_changes_nothing_else() -> None:
    user = UserFactory()
    first = services.save_track(user, Track(name="One"))
    second = services.save_track(user, Track(name="Two"))
    services.delete_track(user, second)
    first.refresh_from_db()
    assert first.is_default


def test_deleting_the_last_track_leaves_none() -> None:
    user = UserFactory()
    track = services.save_track(user, Track(name="One"))
    services.delete_track(user, track)
    assert not Track.objects.for_user(user).exists()


def test_the_database_refuses_two_default_tracks() -> None:
    user = UserFactory()
    TrackFactory(user=user, is_default=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        TrackFactory(user=user, is_default=True)
    TrackFactory(user=UserFactory(), is_default=True)  # another user may have their own


# --- completeness ----------------------------------------------------------------------------------------


def test_an_empty_profile_scores_zero_with_tips() -> None:
    result = services.completeness(UserFactory())
    assert result.percent == 0
    joined = " ".join(result.tips)
    assert "headline" in joined and "metrics" in joined and "8 skills" in joined


def test_a_full_profile_scores_100_with_no_tips() -> None:
    user = UserFactory()
    profile = services.get_profile(user)
    profile.headline, profile.summary, profile.public_email = "H", "S", "a@example.com"
    profile.save()
    exp = ExperienceFactory(user=user)
    BulletFactory(experience=exp, metrics="30 tickets a day")
    for i in range(8):
        SkillFactory(user=user, name=f"S{i}")
    EducationFactory(user=user)
    ProjectFactory(user=user, description="Built a thing")
    TrackFactory(user=user)
    result = services.completeness(user)
    assert result.percent == 100 and result.tips == []


def test_metrics_and_skill_count_drive_their_tips() -> None:
    user = UserFactory()
    exp = ExperienceFactory(user=user)
    BulletFactory(experience=exp, metrics="")
    for i in range(7):
        SkillFactory(user=user, name=f"S{i}")
    tips = " ".join(services.completeness(user).tips)
    assert "metrics" in tips and "8 skills" in tips
    SkillFactory(user=user, name="S8")
    BulletFactory(experience=exp, metrics="12 per day")
    tips = " ".join(services.completeness(user).tips)
    assert "metrics" not in tips and "8 skills" not in tips


# --- serialiser --------------------------------------------------------------------------------------------


@pytest.fixture
def rich_profile() -> Any:
    user = UserFactory()
    profile = services.get_profile(user)
    profile.headline, profile.summary = "Support engineer", "Helps people."
    profile.years_experience = Decimal("4.5")
    profile.links = [{"label": "Site", "url": "https://x.example"}]
    profile.save()
    python = SkillFactory(
        user=user, name="Python", category="technical", level="expert", years=Decimal("5")
    )
    sql = SkillFactory(user=user, name="SQL", category="technical", level="basic")
    exp = ExperienceFactory(
        user=user, title="Engineer", company="Acme", order=0,
        start_date=datetime.date(2021, 3, 1), end_date=None,
    )  # fmt: skip
    bullet = BulletFactory(experience=exp, text="Fixed bugs", metrics="40 per week", order=0)
    bullet.skills.set([python, sql])
    project = ProjectFactory(user=user, name="Tool", description="A tool", order=0)
    project.technologies.set([python])
    EducationFactory(user=user, institution="Uni", qualification="BSc", order=0)
    CertificationFactory(user=user, name="Cert", status="earned", order=0)
    return user


def test_serialised_ids_are_stable_and_prefixed(rich_profile: Any) -> None:
    data = serializers.serialize_profile(rich_profile)
    exp = data["experiences"][0]
    bullet = exp["bullets"][0]
    assert exp["id"] == f"EXP-{Experience.objects.get().pk}"
    assert bullet["id"] == f"BUL-{ExperienceBullet.objects.get().pk}"
    assert data["projects"][0]["id"] == f"PRJ-{Project.objects.get().pk}"
    assert data["education"][0]["id"] == f"EDU-{Education.objects.get().pk}"
    assert data["certifications"][0]["id"] == f"CRT-{Certification.objects.get().pk}"
    assert {s["id"] for s in data["skills"]} == {f"SKL-{s.pk}" for s in Skill.objects.all()}


def test_serialiser_snapshot(rich_profile: Any) -> None:
    data = serializers.serialize_profile(rich_profile)
    exp_pk = Experience.objects.get().pk
    bullet_pk = ExperienceBullet.objects.get().pk
    assert data["basics"] == {
        "headline": "Support engineer", "summary": "Helps people.", "phone": "", "email": "",
        "location": "", "links": [{"label": "Site", "url": "https://x.example"}],
        "years_experience": 4.5,
    }  # fmt: skip
    assert data["experiences"] == [
        {
            "id": f"EXP-{exp_pk}", "title": "Engineer", "company": "Acme", "location": "",
            "employment_type": "", "start": "2021-03", "end": None, "current": True,
            "description": "",
            "bullets": [
                {
                    "id": f"BUL-{bullet_pk}", "text": "Fixed bugs", "metrics": "40 per week",
                    "skills": ["Python", "SQL"],
                }
            ],
        }
    ]  # fmt: skip
    assert [s["name"] for s in data["skills"]] == ["Python", "SQL"]
    assert data["projects"][0]["technologies"] == ["Python"]
    assert list(data) == [
        "version",
        "basics",
        "experiences",
        "projects",
        "education",
        "certifications",
        "skills",
    ]  # fixed key order


def test_serialiser_output_is_deterministic(rich_profile: Any) -> None:
    assert serializers.serialize_profile(rich_profile) == serializers.serialize_profile(
        rich_profile
    )


def test_serialiser_follows_user_ordering(rich_profile: Any) -> None:
    second = ExperienceFactory(user=rich_profile, title="Later job", order=1)
    services.move_item(rich_profile, second, "up")
    titles = [e["title"] for e in serializers.serialize_profile(rich_profile)["experiences"]]
    assert titles == ["Later job", "Engineer"]


def test_serialiser_only_includes_the_users_own_data(rich_profile: Any) -> None:
    stranger = UserFactory()
    ExperienceFactory(user=stranger, title="Secret job")
    SkillFactory(user=stranger, name="Secret skill")
    text = str(serializers.serialize_profile(rich_profile))
    assert "Secret" not in text
    assert serializers.serialize_profile(stranger)["experiences"][0]["title"] == "Secret job"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("EXP-12", ("EXP", 12)),
        (" BUL-8 ", ("BUL", 8)),
        ("XYZ-1", None),
        ("EXP-", None),
        ("exp-1", None),
        ("EXP-1-2", None),
    ],
)
def test_parse_ref(value: str, expected: tuple[str, int] | None) -> None:
    assert serializers.parse_ref(value) == expected


# --- summary ---------------------------------------------------------------------------------------------


def test_summary_is_deterministic_and_compact(rich_profile: Any) -> None:
    summary = serializers.profile_summary(rich_profile)
    assert summary == serializers._build_summary(serializers.serialize_profile(rich_profile))
    lines = summary.splitlines()
    assert lines[0] == "Headline: Support engineer"
    assert "Years of experience: 4.5" in lines
    assert "Skills: Python, SQL" in lines  # strongest first
    assert "- Engineer at Acme (2021-03 to present)" in lines
    assert "Education: BSc, Uni" in lines and "Certifications: Cert" in lines
    assert "Fixed bugs" not in summary  # bullets are not in the compact summary


def test_summary_is_cached_per_version_and_rebuilt_after_a_change(
    rich_profile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = serializers._build_summary

    def counting(data: dict[str, Any]) -> str:
        calls.append(1)
        return real(data)

    monkeypatch.setattr(serializers, "_build_summary", counting)
    cache.clear()
    first = serializers.profile_summary(rich_profile)
    serializers.profile_summary(rich_profile)
    assert len(calls) == 1  # second call came from the cache

    services.save_item(rich_profile, Experience(title="New role", company="NewCo"))
    changed = serializers.profile_summary(rich_profile)
    assert len(calls) == 2 and "New role" in changed and changed != first


def test_an_empty_profile_has_an_empty_summary() -> None:
    assert serializers.profile_summary(UserFactory()) == ""
    assert Profile.objects.count() == 1  # created on demand
