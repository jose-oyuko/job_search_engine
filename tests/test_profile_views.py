"""Profile editor (HTMX), tracks, CV import screens and the onboarding wizard."""

import datetime
import io
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.accounts.models import UserSettings
from apps.llm.providers.fake import FakeProvider
from apps.profiles import onboarding, services
from apps.profiles.models import (
    CVImport,
    Education,
    Experience,
    ExperienceBullet,
    Profile,
    Skill,
    Track,
)
from tests import files
from tests.factories import (
    BulletFactory,
    ExperienceFactory,
    SkillFactory,
    TrackFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def user() -> Any:
    user = UserFactory()
    UserSettings.objects.create(user=user, onboarding_completed=True)
    return user


@pytest.fixture
def client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _upload(name: str, content: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/octet-stream")


# --- the profile page --------------------------------------------------------------------------------


def test_profile_page_requires_login() -> None:
    response = Client().get(reverse("profiles:profile"))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_profile_page_renders_all_sections_and_the_meter(client: Client) -> None:
    html = client.get("/app/profile/").content.decode()
    for heading in ("Experience", "Skills", "Projects", "Education", "Certifications"):
        assert f">{heading}<" in html
    assert 'value="0" max="100"' in html and "Add a headline" in html
    assert 'hx-get="/app/profile/experience/new/"' in html
    assert "/app/profile/import/" in html and "/app/profile/tracks/" in html


def test_profile_nav_link_is_live(client: Client) -> None:
    assert 'href="/app/profile/"' in client.get("/app/settings/account/").content.decode()


def test_basics_form_saves_and_bumps_the_version(client: Client, user: Any) -> None:
    before = services.get_profile(user).version
    response = client.post(
        "/app/profile/basics/",
        {
            "headline": "Support engineer",
            "summary": "Hello",
            "public_email": "me@example.com",
            "years_experience": "3.5",
            "links_text": "Site | https://x.example\nhttps://bare.example",
        },
    )
    assert response.status_code == 302
    profile = Profile.objects.get(user=user)
    assert profile.headline == "Support engineer" and profile.years_experience == 3.5
    assert profile.links == [
        {"label": "Site", "url": "https://x.example"},
        {"label": "", "url": "https://bare.example"},
    ]
    assert profile.version == before + 1


def test_basics_rejects_bad_links(client: Client) -> None:
    response = client.post("/app/profile/basics/", {"links_text": "javascript:alert(1)"})
    assert response.status_code == 200 and b"not a valid web address" in response.content


# --- inline CRUD (HTMX) -----------------------------------------------------------------------------


def test_new_form_is_an_htmx_partial(client: Client) -> None:
    response = client.get("/app/profile/experience/new/", **HX)
    html = response.content.decode()
    assert response.status_code == 200 and "<html" not in html
    assert 'hx-post="/app/profile/experience/"' in html and 'type="month"' in html
    assert 'id="new-experience"' in html


def test_plain_get_of_a_partial_redirects_to_the_page(client: Client) -> None:
    assert client.get("/app/profile/experience/new/")["Location"] == "/app/profile/"


def test_cancel_returns_the_empty_placeholder(client: Client) -> None:
    html = client.get("/app/profile/experience/new/?cancel=1", **HX).content.decode()
    assert html == '<div id="new-experience"></div>'


def test_create_returns_the_row_with_htmx_retarget_headers(client: Client, user: Any) -> None:
    response = client.post(
        "/app/profile/experience/",
        {"title": "Engineer", "company": "Acme", "start_date": "2021-03", "end_date": ""},
        **HX,
    )
    html = response.content.decode()
    experience = Experience.objects.get(user=user)
    assert response.status_code == 200
    assert experience.start_date == datetime.date(2021, 3, 1) and experience.end_date is None
    assert f'id="item-experience-{experience.pk}"' in html and "Engineer" in html
    assert response["HX-Retarget"] == "#list-experience" and response["HX-Reswap"] == "beforeend"
    assert 'id="new-experience" hx-swap-oob="true"' in html  # clears the form container


def test_invalid_create_redisplays_the_form_with_errors(client: Client, user: Any) -> None:
    response = client.post("/app/profile/experience/", {"title": "", "company": "Acme"}, **HX)
    html = response.content.decode()
    assert response.status_code == 200 and "This field is required" in html
    assert "HX-Retarget" not in response
    assert not Experience.objects.filter(user=user).exists()


def test_end_before_start_is_rejected(client: Client) -> None:
    data = {"title": "T", "company": "C", "start_date": "2022-05", "end_date": "2021-01"}
    html = client.post("/app/profile/experience/", data, **HX).content.decode()
    assert "cannot be before" in html


def test_edit_save_and_delete_cycle(client: Client, user: Any) -> None:
    exp = ExperienceFactory(user=user, title="Old title")
    edit = client.get(f"/app/profile/experience/{exp.pk}/edit/", **HX).content.decode()
    assert "Old title" in edit and f'hx-post="/app/profile/experience/{exp.pk}/save/"' in edit

    saved = client.post(
        f"/app/profile/experience/{exp.pk}/save/",
        {"title": "New title", "company": "Acme", "start_date": "2021-03"},
        **HX,
    )
    assert "New title" in saved.content.decode() and "HX-Retarget" not in saved
    exp.refresh_from_db()
    assert exp.title == "New title"

    shown = client.get(f"/app/profile/experience/{exp.pk}/", **HX).content.decode()
    assert "New title" in shown and 'hx-get="/app/profile/experience/' in shown

    deleted = client.post(f"/app/profile/experience/{exp.pk}/delete/", **HX)
    assert deleted.status_code == 200 and deleted.content == b""
    assert not Experience.objects.filter(pk=exp.pk).exists()


def test_reorder_persists_and_returns_the_list(client: Client, user: Any) -> None:
    a = ExperienceFactory(user=user, title="A", order=0)
    b = ExperienceFactory(user=user, title="B", order=1)
    response = client.post(f"/app/profile/experience/{b.pk}/move/", {"direction": "up"}, **HX)
    html = response.content.decode()
    assert html.index("B") < html.index("A") and 'id="list-experience"' in html
    assert [e.title for e in Experience.objects.for_user(user)] == ["B", "A"]
    assert client.get("/app/profile/").content.decode().index(">B<") < 10**9
    assert a.pk != b.pk


def test_move_rejects_bad_direction_and_unordered_sections(client: Client, user: Any) -> None:
    exp = ExperienceFactory(user=user)
    skill = SkillFactory(user=user)
    assert (
        client.post(
            f"/app/profile/experience/{exp.pk}/move/", {"direction": "no"}, **HX
        ).status_code
        == 400
    )
    assert (
        client.post(f"/app/profile/skill/{skill.pk}/move/", {"direction": "up"}, **HX).status_code
        == 404
    )


def test_unknown_section_is_404(client: Client) -> None:
    assert client.get("/app/profile/nonsense/new/", **HX).status_code == 404
    assert client.post("/app/profile/nonsense/", {}, **HX).status_code == 404


def test_bullets_are_created_inside_their_experience(client: Client, user: Any) -> None:
    exp = ExperienceFactory(user=user)
    skill = SkillFactory(user=user, name="Python")
    form = client.get(f"/app/profile/bullet/new/?experience={exp.pk}", **HX).content.decode()
    assert "Put the real numbers here" in form and f'name="experience" value="{exp.pk}"' in form
    assert f'id="new-bullet-{exp.pk}"' in form and "Python" in form

    response = client.post(
        "/app/profile/bullet/",
        {
            "experience": exp.pk,
            "text": "Fixed bugs",
            "metrics": "40 per week",
            "skills": [skill.pk],
        },
        **HX,
    )
    bullet = ExperienceBullet.objects.get(user=user)
    assert bullet.experience == exp and list(bullet.skills.all()) == [skill]
    assert response["HX-Retarget"] == f"#list-bullet-{exp.pk}"
    assert "Metrics: 40 per week" in response.content.decode()


def test_the_profile_page_shows_bullets_under_their_experience(client: Client, user: Any) -> None:
    exp = ExperienceFactory(user=user, title="Engineer")
    BulletFactory(experience=exp, text="Unique bullet text", metrics="")
    html = client.get("/app/profile/").content.decode()
    assert "Unique bullet text" in html and "No metrics yet" in html
    assert f'id="list-bullet-{exp.pk}"' in html


def test_skill_names_must_be_unique_in_the_form(client: Client, user: Any) -> None:
    SkillFactory(user=user, name="Python")
    html = client.post(
        "/app/profile/skill/", {"name": "python", "category": "technical"}, **HX
    ).content.decode()
    assert "already have a skill" in html and Skill.objects.filter(user=user).count() == 1


def test_project_education_and_certification_forms(client: Client, user: Any) -> None:
    skill = SkillFactory(user=user)
    client.post(
        "/app/profile/project/",
        {"name": "Tool", "technologies": [skill.pk], "highlights": "a, b\nc", "url": "example.com"},
        **HX,
    )
    client.post("/app/profile/education/", {"institution": "Uni", "end_date": "2020-06"}, **HX)
    client.post("/app/profile/certification/", {"name": "A+", "status": "earned"}, **HX)
    from apps.profiles.models import Certification, Project

    project = Project.objects.get(user=user)
    assert project.highlights == ["a", "b", "c"] and project.url == "https://example.com"
    assert list(project.technologies.all()) == [skill]
    assert Education.objects.get(user=user).end_date == datetime.date(2020, 6, 1)
    assert Certification.objects.get(user=user).name == "A+"


# --- tracks -------------------------------------------------------------------------------------------------


def test_track_crud_and_default_handling(client: Client, user: Any) -> None:
    form = {
        "name": "IT support",
        "target_titles": "Helpdesk, IT Support\nSupport Engineer",
        "work_types": ["remote", "hybrid"],
        "employment_types": ["full_time"],
        "min_pay_amount": "15.50",
        "min_pay_currency": "usd",
        "min_pay_period": "hour",
        "exclude_keywords": "senior",
    }
    assert client.post("/app/profile/tracks/new/", form).status_code == 302
    first = Track.objects.get(user=user)
    assert first.is_default and first.target_titles == [
        "Helpdesk",
        "IT Support",
        "Support Engineer",
    ]
    assert first.work_types == ["remote", "hybrid"] and first.min_pay_currency == "USD"

    client.post("/app/profile/tracks/new/", {**form, "name": "Backend"})
    second = Track.objects.get(name="Backend")
    assert not second.is_default

    client.post(f"/app/profile/tracks/{second.pk}/default/")
    first.refresh_from_db()
    second.refresh_from_db()
    assert second.is_default and not first.is_default

    client.post(f"/app/profile/tracks/{second.pk}/delete/")
    first.refresh_from_db()
    assert first.is_default and Track.objects.for_user(user).count() == 1

    page = client.get("/app/profile/tracks/").content.decode()
    assert "IT support" in page and "Default" in page


def test_track_form_validation(client: Client) -> None:
    bad = {"name": "X", "min_pay_currency": "us1", "work_types": ["moon"]}
    html = client.post("/app/profile/tracks/new/", bad).content.decode()
    assert "3-letter currency" in html and "valid choice" in html
    assert not Track.objects.exists()


def test_track_edit_prefills_and_saves(client: Client, user: Any) -> None:
    track = TrackFactory(user=user, name="Mine", target_titles=["A", "B"], is_default=True)
    page = client.get(f"/app/profile/tracks/{track.pk}/").content.decode()
    assert "A, B" in page
    client.post(
        f"/app/profile/tracks/{track.pk}/",
        {
            "name": "Renamed",
            "target_titles": "Z",
            "min_pay_currency": "USD",
            "min_pay_period": "hour",
            "make_default": "on",
        },
    )
    track.refresh_from_db()
    assert track.name == "Renamed" and track.target_titles == ["Z"] and track.is_default


# --- CV import screens --------------------------------------------------------------------------------------------


def test_upload_pdf_review_and_apply_end_to_end(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            "/app/profile/import/", {"file": _upload("cv.pdf", files.pdf_bytes())}
        )
    cv = CVImport.objects.get(user=user)
    assert response["Location"] == f"/app/profile/import/{cv.pk}/"

    review = client.get(response["Location"]).content.decode()
    assert "IT Support Officer" in review and "Acme Ltd" in review and "Active Directory" in review
    assert 'name="exp" value="0"' in review and 'value="replace" checked' in review

    applied = client.post(
        f"/app/profile/import/{cv.pk}/apply/",
        {
            "mode": "replace",
            "headline": "My edited headline",
            "exp": ["0"],  # untick Beta Networks
            "edu": ["0"],
            "skill": ["0", "1"],
        },
        follow=True,
    )
    html = applied.content.decode()
    assert applied.redirect_chain[-1][0] == "/app/profile/"
    assert "Imported 1 roles, 2 skills" in html and "My edited headline" in html
    assert [e.company for e in Experience.objects.for_user(user)] == ["Acme Ltd"]
    assert Skill.objects.for_user(user).count() == 2
    assert "Resolved helpdesk tickets" in html  # the bullets are visible in the editor


def test_upload_docx_works_the_same_way(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("cv.docx", files.docx_bytes())})
    cv = CVImport.objects.get(user=user)
    assert cv.status == CVImport.Status.PARSED
    client.post(f"/app/profile/import/{cv.pk}/apply/", {"mode": "merge"})
    assert Experience.objects.for_user(user).count() == 0  # nothing ticked means nothing added


def test_apply_without_selection_ticks_nothing_but_all_ticked_adds_everything(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    cv = CVImport.objects.get(user=user)
    client.post(
        f"/app/profile/import/{cv.pk}/apply/",
        {
            "mode": "replace",
            "exp": ["0", "1"],
            "proj": ["0"],
            "edu": ["0"],
            "cert": ["0"],
            "skill": ["0", "1", "2", "3"],
        },
    )
    assert (
        Experience.objects.for_user(user).count() == 2 and Skill.objects.for_user(user).count() == 4
    )


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("scan.pdf", files.blank_pdf_bytes(), "looks like a scan"),
        ("cv.txt", b"plain text", "PDF or DOCX"),
        ("cv.pdf", b"MZ-this-is-an-exe", "real PDF"),
        ("cv.docx", files.pdf_bytes(), "real DOCX"),
    ],
    ids=["scanned", "wrong-extension", "bad-magic", "pdf-as-docx"],
)
def test_bad_uploads_show_a_friendly_error(
    client: Client, user: Any, name: str, content: bytes, message: str
) -> None:
    response = client.post("/app/profile/import/", {"file": _upload(name, content)})
    assert response.status_code == 200 and message in response.content.decode()
    assert not CVImport.objects.filter(user=user).exists()


def test_oversized_upload_is_refused(client: Client, settings) -> None:  # type: ignore[no-untyped-def]
    settings.CV_MAX_UPLOAD_BYTES = 500
    response = client.post("/app/profile/import/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    assert "too large" in response.content.decode()


def test_missing_file_is_a_form_error(client: Client) -> None:
    response = client.post("/app/profile/import/", {})
    assert response.status_code == 200 and "required" in response.content.decode()


def test_uploads_are_rate_limited_per_user(client: Client) -> None:
    codes = [
        client.post("/app/profile/import/", {"file": _upload("cv.txt", b"x")}).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_a_parsing_import_shows_the_waiting_state_and_polls(client: Client, user: Any) -> None:
    cv = CVImport(user=user, status=CVImport.Status.PARSING, original_name="cv.pdf")
    cv.file.save("cv.pdf", io.BytesIO(files.pdf_bytes()))  # type: ignore[arg-type]
    cv.save()
    page = client.get(f"/app/profile/import/{cv.pk}/").content.decode()
    assert "Reading your CV" in page and f"/app/profile/import/{cv.pk}/status/" in page
    poll = client.get(f"/app/profile/import/{cv.pk}/status/")
    assert poll.status_code == 200 and "Reading your CV" in poll.content.decode()

    CVImport.objects.filter(pk=cv.pk).update(status=CVImport.Status.PARSED)
    done = client.get(f"/app/profile/import/{cv.pk}/status/")
    assert done["HX-Refresh"] == "true"


def test_a_failed_import_explains_and_offers_another_try(
    client: Client, user: Any, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["bad", "worse"])
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    cv = CVImport.objects.get(user=user)
    html = client.get(f"/app/profile/import/{cv.pk}/").content.decode()
    assert (
        "could not read this CV" in html
        and "unusable answer" in html
        and "Try another file" in html
    )


def test_invalid_review_basics_redisplays_the_review(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    cv = CVImport.objects.get(user=user)
    response = client.post(
        f"/app/profile/import/{cv.pk}/apply/", {"mode": "replace", "public_email": "nope"}
    )
    assert response.status_code == 400
    cv.refresh_from_db()
    assert cv.status == CVImport.Status.PARSED


def test_next_url_must_be_inside_the_wizard(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    cv = CVImport.objects.get(user=user)
    response = client.post(
        f"/app/profile/import/{cv.pk}/apply/", {"mode": "replace", "next": "https://evil.example/"}
    )
    assert response["Location"] == "/app/profile/"


def test_applying_twice_reports_a_message_not_an_error(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    cv = CVImport.objects.get(user=user)
    client.post(f"/app/profile/import/{cv.pk}/apply/", {"mode": "replace"})
    again = client.post(f"/app/profile/import/{cv.pk}/apply/", {"mode": "replace"}, follow=True)
    assert "cannot be applied" in again.content.decode()


def test_original_file_download_is_an_attachment(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    pdf = files.pdf_bytes()
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("My CV.pdf", pdf)})
    cv = CVImport.objects.get(user=user)
    response = client.get(f"/app/profile/import/{cv.pk}/file/")
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment")
    assert b"".join(response.streaming_content) == pdf


def test_the_upload_page_lists_recent_uploads(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/profile/import/", {"file": _upload("mycv.pdf", files.pdf_bytes())})
    html = client.get("/app/profile/import/").content.decode()
    assert "mycv.pdf" in html and "Ready to review" in html


# --- onboarding wizard ------------------------------------------------------------------------------------------


@pytest.fixture
def new_client() -> tuple[Client, Any]:
    user = UserFactory()
    UserSettings.objects.create(user=user)  # onboarding not completed
    client = Client()
    client.force_login(user)
    return client, user


def test_dashboard_redirects_to_the_wizard_until_onboarding_is_done(new_client) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    assert client.get("/app/")["Location"] == "/welcome/"
    assert client.get("/welcome/")["Location"] == "/welcome/cv/"
    UserSettings.objects.filter(user=user).update(onboarding_completed=True)
    assert client.get("/app/").status_code == 200


def test_users_without_a_settings_row_are_not_forced_through_the_wizard() -> None:
    client = Client()
    client.force_login(UserFactory())
    assert client.get("/app/").status_code == 200


def test_wizard_requires_login() -> None:
    assert "/accounts/login/" in Client().get("/welcome/cv/")["Location"]


def test_full_manual_wizard_path(new_client) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    cv_page = client.get("/welcome/cv/").content.decode()
    assert "Step 1 of 5" in cv_page and "I will fill it in myself" in cv_page

    review = client.get("/welcome/review/").content.decode()  # no CV: manual basics
    assert "Step 2 of 5" in review and 'name="headline"' in review
    assert (
        client.post("/welcome/review/", {"headline": "Data labeller"})["Location"]
        == "/welcome/target/"
    )
    assert Profile.objects.get(user=user).headline == "Data labeller"

    target = client.get("/welcome/target/").content.decode()
    assert 'value="My job search"' in target
    response = client.post(
        "/welcome/target/",
        {
            "name": "AI training",
            "target_titles": "Data annotator",
            "work_types": ["remote"],
            "min_pay_currency": "USD",
            "min_pay_period": "hour",
        },
    )
    assert response["Location"] == "/welcome/location/"
    track = Track.objects.get(user=user)
    assert track.is_default and track.target_titles == ["Data annotator"]

    location = client.get("/welcome/location/").content.decode()
    assert "Africa/Nairobi" in location and 'value="KE" selected' in location
    response = client.post("/welcome/location/", {"country": "UG", "timezone": "Africa/Kampala"})
    assert response["Location"] == "/welcome/notifications/"
    settings_row = UserSettings.objects.get(user=user)
    assert (settings_row.country, settings_row.timezone) == ("UG", "Africa/Kampala")

    alerts = client.get("/welcome/notifications/").content.decode()
    assert (
        "Step 5 of 5" in alerts and 'name="notify_mode"' in alerts and "Connect Telegram" in alerts
    )
    response = client.post(
        "/welcome/notifications/",
        {"notify_mode": "daily", "digest_time": "07:30", "notify_email": "on"},
    )
    assert response["Location"] == "/welcome/done/"
    settings_row.refresh_from_db()
    assert settings_row.notify_mode == "daily" and settings_row.digest_time.hour == 7

    done = client.get("/welcome/done/", follow=True)
    assert done.redirect_chain[-1][0] == "/app/" and "all set" in done.content.decode()
    assert UserSettings.objects.get(user=user).onboarding_completed


def test_returning_to_the_target_step_edits_the_same_track(new_client) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    data = {
        "name": "One",
        "target_titles": "A",
        "min_pay_currency": "USD",
        "min_pay_period": "hour",
    }
    client.post("/welcome/target/", data)
    client.post("/welcome/target/", {**data, "name": "Two"})
    assert list(Track.objects.for_user(user).values_list("name", flat=True)) == ["Two"]


def test_wizard_validates_location(new_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = new_client
    response = client.post("/welcome/location/", {"country": "XX", "timezone": "Mars/Base"})
    assert response.status_code == 200 and b"valid choice" in response.content


def test_wizard_cv_path_uploads_reviews_and_continues(
    new_client, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/welcome/cv/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    assert response["Location"] == "/welcome/review/"

    review = client.get("/welcome/review/").content.decode()
    assert "IT Support Officer" in review and 'name="next" value="/welcome/target/"' in review

    applied = client.post(
        f"/app/profile/import/{CVImport.objects.get(user=user).pk}/apply/",
        {"mode": "replace", "next": "/welcome/target/", "exp": ["0"]},
    )
    assert applied["Location"] == "/welcome/target/"
    assert Experience.objects.for_user(user).count() == 1
    # Once applied, the review step falls back to the manual basics form.
    assert 'name="headline"' in client.get("/welcome/review/").content.decode()


def test_wizard_shows_waiting_then_failure_states(
    new_client, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    with django_capture_on_commit_callbacks(execute=False):
        client.post("/welcome/cv/", {"file": _upload("cv.pdf", files.pdf_bytes())})
    waiting = client.get("/welcome/review/").content.decode()
    assert "Reading your CV" in waiting and "Skip for now" in waiting

    CVImport.objects.filter(user=user).update(
        status=CVImport.Status.FAILED, error="Parsing failed."
    )
    failed = client.get("/welcome/review/").content.decode()
    assert "Parsing failed." in failed and 'name="headline"' in failed


def test_wizard_cv_step_rejects_bad_files(new_client) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    response = client.post("/welcome/cv/", {"file": _upload("scan.pdf", files.blank_pdf_bytes())})
    assert response.status_code == 200 and "scan" in response.content.decode()
    assert not CVImport.objects.filter(user=user).exists()


def test_skip_setup_completes_onboarding(new_client) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    assert "/welcome/done/" in client.get("/welcome/cv/").content.decode()
    client.get("/welcome/done/")
    assert UserSettings.objects.get(user=user).onboarding_completed


def test_unknown_wizard_step_is_404(new_client) -> None:  # type: ignore[no-untyped-def]
    assert new_client[0].get("/welcome/bogus/").status_code == 404


def test_new_steps_can_be_appended_and_the_wizard_adapts(
    new_client, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from django.http import HttpResponse

    extra = onboarding.Step("alerts-extra", "Extra", lambda r: HttpResponse("extra step"))
    steps = [*onboarding.STEPS, extra]
    monkeypatch.setattr(onboarding, "STEPS", steps)
    monkeypatch.setattr(onboarding, "STEP_BY_SLUG", {s.slug: s for s in steps})

    assert onboarding.next_slug("notifications") == "alerts-extra"
    assert onboarding.next_slug("alerts-extra") == "done"
    client, _ = new_client
    assert client.get("/welcome/alerts-extra/").content == b"extra step"
    assert "Step 4 of 6" in client.get("/welcome/location/").content.decode()


def test_signup_to_dashboard_happy_path_uses_the_wizard(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    from apps.accounts.services import complete_signup

    user = UserFactory()
    complete_signup(user, None)
    client = Client()
    client.force_login(user)
    assert client.get("/app/")["Location"] == "/welcome/"


def test_profile_changes_are_visible_in_the_editor_after_wizard(new_client) -> None:  # type: ignore[no-untyped-def]
    client, user = new_client
    ExperienceFactory(user=user, title="Visible role")
    assert "Visible role" in client.get("/app/profile/").content.decode()
