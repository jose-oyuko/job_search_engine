"""Multi-user isolation for everything Phase 3 adds (spec §16).

User A must get a 404 for every detail, edit, save, delete, move, download and apply URL that
belongs to user B, and must never see B's data in a page, a form choice or the serialiser.
"""

from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.test import Client

from apps.accounts.models import UserSettings
from apps.profiles import serializers, services
from apps.profiles.models import (
    Certification,
    CVImport,
    Education,
    Experience,
    ExperienceBullet,
    Profile,
    Project,
    Skill,
    Track,
)
from tests import files
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
HX = {"HTTP_HX_REQUEST": "true"}

ITEM_FACTORIES = {
    "experience": ExperienceFactory,
    "bullet": BulletFactory,
    "skill": SkillFactory,
    "project": ProjectFactory,
    "education": EducationFactory,
    "certification": CertificationFactory,
}
ORDERED = ["experience", "bullet", "project", "education", "certification"]


@pytest.fixture
def a_and_b() -> tuple[Client, Any, Any]:
    a, b = UserFactory(), UserFactory()
    for user in (a, b):
        UserSettings.objects.create(user=user, onboarding_completed=True)
    client = Client()
    client.force_login(a)
    return client, a, b


def _b_item(section: str, b: Any) -> Any:
    factory = ITEM_FACTORIES[section]
    if section == "bullet":
        return factory(experience=ExperienceFactory(user=b))
    return factory(user=b)


@pytest.mark.parametrize("section", list(ITEM_FACTORIES))
@pytest.mark.parametrize("action", ["show", "edit"])
def test_other_users_items_cannot_be_viewed_or_edited(a_and_b, section: str, action: str) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    item = _b_item(section, b)
    suffix = "" if action == "show" else "edit/"
    assert client.get(f"/app/profile/{section}/{item.pk}/{suffix}", **HX).status_code == 404


@pytest.mark.parametrize("section", list(ITEM_FACTORIES))
def test_other_users_items_cannot_be_saved_or_deleted(a_and_b, section: str) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    item = _b_item(section, b)
    model = type(item)
    before = model.objects.get(pk=item.pk)
    version = Profile.objects.get_or_create(user=b)[0].version

    save = client.post(
        f"/app/profile/{section}/{item.pk}/save/",
        {"name": "hacked", "title": "hacked", "text": "hacked"},
        **HX,
    )
    delete = client.post(f"/app/profile/{section}/{item.pk}/delete/", **HX)

    assert save.status_code == 404 and delete.status_code == 404
    assert model.objects.filter(pk=item.pk).exists()
    after = model.objects.get(pk=item.pk)
    assert after.updated_at == before.updated_at
    assert Profile.objects.get(user=b).version == version  # no bump for B either


@pytest.mark.parametrize("section", ORDERED)
def test_other_users_items_cannot_be_reordered(a_and_b, section: str) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    item = _b_item(section, b)
    response = client.post(f"/app/profile/{section}/{item.pk}/move/", {"direction": "up"}, **HX)
    assert response.status_code == 404


def test_a_bullet_cannot_be_created_under_another_users_experience(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    theirs = ExperienceFactory(user=b)
    assert client.get(f"/app/profile/bullet/new/?experience={theirs.pk}", **HX).status_code == 404
    response = client.post(
        "/app/profile/bullet/", {"experience": theirs.pk, "text": "sneaky"}, **HX
    )
    assert response.status_code == 404
    assert not ExperienceBullet.objects.exists()


def test_a_bullet_cannot_be_moved_to_another_users_experience_on_edit(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    mine = BulletFactory(experience=ExperienceFactory(user=a))
    theirs = ExperienceFactory(user=b)
    response = client.post(
        f"/app/profile/bullet/{mine.pk}/save/", {"experience": theirs.pk, "text": "moved"}, **HX
    )
    mine.refresh_from_db()
    assert mine.experience.user == a
    assert response.status_code == 200 and "valid choice" in response.content.decode()


def test_skill_choices_never_include_other_users_skills(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    SkillFactory(user=a, name="MySkill")
    SkillFactory(user=b, name="SecretSkill")
    exp = ExperienceFactory(user=a)
    for url in (f"/app/profile/bullet/new/?experience={exp.pk}", "/app/profile/project/new/"):
        html = client.get(url, **HX).content.decode()
        assert "MySkill" in html and "SecretSkill" not in html


def test_other_users_skill_ids_are_rejected_when_posted(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    theirs = SkillFactory(user=b)
    response = client.post(
        "/app/profile/project/", {"name": "P", "technologies": [theirs.pk]}, **HX
    )
    assert "valid choice" in response.content.decode()
    assert not Project.objects.filter(user=a).exists()


def test_the_profile_page_only_shows_own_data(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    ExperienceFactory(user=a, title="My role")
    ExperienceFactory(user=b, title="Their role")
    BulletFactory(experience=ExperienceFactory(user=b), text="Their bullet")
    SkillFactory(user=b, name="Their skill")
    ProjectFactory(user=b, name="Their project")
    EducationFactory(user=b, institution="Their uni")
    CertificationFactory(user=b, name="Their cert")
    services.get_profile(b).headline = "x"
    profile_b = services.get_profile(b)
    profile_b.headline = "Their headline"
    profile_b.save()

    html = client.get("/app/profile/").content.decode()
    assert "My role" in html and "Their" not in html


def test_basics_form_edits_only_the_requesting_users_profile(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    profile_b = services.get_profile(b)
    profile_b.headline = "B headline"
    profile_b.save()
    client.post("/app/profile/basics/", {"headline": "A headline"})
    assert Profile.objects.get(user=a).headline == "A headline"
    assert Profile.objects.get(user=b).headline == "B headline"


# --- tracks --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["", "delete/", "default/"])
def test_other_users_tracks_cannot_be_read_changed_or_deleted(a_and_b, action: str) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    track = TrackFactory(user=b, is_default=True, name="Theirs")
    url = f"/app/profile/tracks/{track.pk}/{action}"
    response = client.post(url, {"name": "hacked"}) if action else client.get(url)
    assert response.status_code == 404
    track.refresh_from_db()
    assert track.name == "Theirs" and track.is_default


def test_track_list_only_shows_own_tracks(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    TrackFactory(user=a, name="Mine", is_default=True)
    TrackFactory(user=b, name="Secret track", is_default=True)  # both may have a default
    html = client.get("/app/profile/tracks/").content.decode()
    assert "Mine" in html and "Secret track" not in html


def test_making_my_track_default_does_not_touch_other_users(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    mine = TrackFactory(user=a, name="Mine")
    theirs = TrackFactory(user=b, is_default=True)
    client.post(f"/app/profile/tracks/{mine.pk}/default/")
    theirs.refresh_from_db()
    assert theirs.is_default
    assert Track.objects.get(pk=mine.pk).is_default


# --- CV imports ------------------------------------------------------------------------------------------


def _b_import(b: Any, status: str = CVImport.Status.PARSED) -> CVImport:
    cv = CVImport(
        user=b,
        status=status,
        original_name="theirs.pdf",
        parsed_data={
            "headline": "Their secret headline",
            "experiences": [{"title": "T", "company": "C"}],
        },
    )
    cv.file.save("theirs.pdf", ContentFile(files.pdf_bytes()), save=False)
    cv.save()
    return cv


@pytest.mark.parametrize("path", ["", "status/", "file/"])
def test_other_users_cv_imports_cannot_be_read_or_downloaded(a_and_b, path: str) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    cv = _b_import(b)
    assert client.get(f"/app/profile/import/{cv.pk}/{path}").status_code == 404


def test_other_users_cv_imports_cannot_be_applied(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    cv = _b_import(b)
    response = client.post(f"/app/profile/import/{cv.pk}/apply/", {"mode": "replace", "exp": ["0"]})
    assert response.status_code == 404
    cv.refresh_from_db()
    assert cv.status == CVImport.Status.PARSED
    assert not Experience.objects.filter(user=a).exists()


def test_the_upload_page_lists_only_own_imports(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    _b_import(b)
    html = client.get("/app/profile/import/").content.decode()
    assert "theirs.pdf" not in html


def test_the_wizard_review_step_ignores_other_users_imports(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    _b_import(b)
    UserSettings.objects.filter(user=a).update(onboarding_completed=False)
    html = client.get("/welcome/review/").content.decode()
    assert "Their secret headline" not in html and 'name="headline"' in html  # manual form for A


def test_import_files_are_not_publicly_served(a_and_b, settings) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    cv = _b_import(b)
    anonymous = Client()
    assert anonymous.get(f"/app/profile/import/{cv.pk}/file/").status_code == 302  # login required
    assert (
        not hasattr(settings, "MEDIA_URL")
        or not settings.MEDIA_URL
        or cv.file.name not in settings.MEDIA_URL
    )
    assert client.get(f"/media/{cv.file.name}").status_code == 404
    assert client.get(f"/{cv.file.name}").status_code == 404


def test_cv_parse_for_someone_elses_import_still_attributes_llm_usage_to_its_owner(  # type: ignore[no-untyped-def]
    a_and_b, django_capture_on_commit_callbacks
) -> None:
    from apps.llm.models import LLMCall

    _, a, b = a_and_b
    with django_capture_on_commit_callbacks(execute=True):
        services.create_cv_import(b, "cv.pdf", files.pdf_bytes())
    assert LLMCall.objects.get().user == b
    assert not LLMCall.objects.filter(user=a).exists()


# --- model managers and the serialiser -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        Profile,
        Skill,
        Track,
        Experience,
        ExperienceBullet,
        Project,
        Education,
        Certification,
        CVImport,
    ],
)
def test_for_user_is_scoped_for_every_profile_model(model: Any, a_and_b) -> None:  # type: ignore[no-untyped-def]
    _, a, b = a_and_b
    services.get_profile(a)
    services.get_profile(b)
    for user in (a, b):
        exp = ExperienceFactory(user=user)
        BulletFactory(experience=exp)
        SkillFactory(user=user)
        TrackFactory(user=user)
        ProjectFactory(user=user)
        EducationFactory(user=user)
        CertificationFactory(user=user)
        cv = CVImport(user=user)
        cv.file.save("x.pdf", ContentFile(files.pdf_bytes()), save=False)
        cv.save()
    mine = model.objects.for_user(a)
    assert mine.count() == 1 and mine.get().user_id == a.pk
    assert not mine.filter(user=b).exists()


def test_for_user_returns_nothing_for_anonymous_users(a_and_b) -> None:  # type: ignore[no-untyped-def]
    from django.contrib.auth.models import AnonymousUser

    _, _, b = a_and_b
    ExperienceFactory(user=b)
    assert Experience.objects.for_user(AnonymousUser()).count() == 0
    assert CVImport.objects.for_user(None).count() == 0


def test_the_serialiser_and_summary_never_leak_across_users(a_and_b) -> None:  # type: ignore[no-untyped-def]
    _, a, b = a_and_b
    ExperienceFactory(user=a, title="A role")
    exp_b = ExperienceFactory(user=b, title="B secret role")
    BulletFactory(experience=exp_b, text="B secret bullet", metrics="99 things")
    SkillFactory(user=b, name="B secret skill")
    data_a = str(serializers.serialize_profile(a))
    summary_a = serializers.profile_summary(a)
    for text in (data_a, summary_a):
        assert "secret" not in text.lower() and "99 things" not in text
    assert "A role" in data_a and "A role" in summary_a
    assert serializers.profile_summary(a) != serializers.profile_summary(b)


def test_cached_summaries_are_keyed_per_user(a_and_b) -> None:  # type: ignore[no-untyped-def]
    _, a, b = a_and_b
    ExperienceFactory(user=a, title="Alpha role")
    ExperienceFactory(user=b, title="Beta role")
    # Both profiles are at the same version number; the cache must still keep them apart.
    assert services.get_profile(a).version == services.get_profile(b).version
    assert "Alpha" in serializers.profile_summary(a)
    assert "Beta" in serializers.profile_summary(b) and "Alpha" not in serializers.profile_summary(
        b
    )
