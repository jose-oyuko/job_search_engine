"""The board, detail page, stats, manual form and every entry point that creates an application
(spec §14), plus isolation for applications, events and interviews (spec §16)."""

import datetime
from typing import Any

import pytest
from django.test import Client

from apps.matching.models import Match
from apps.notifications import bot_services
from apps.notifications import services as notification_services
from apps.tracker import services
from apps.tracker.models import Application, ApplicationEvent, Interview
from tests.factories import MatchFactory, UserFactory
from tests.kit_helpers import make_kit_job
from tests.notif_helpers import CHAT, make_notif_user, scored_match, utc
from tests.test_tracker_services import NOW, seed

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("pdf_fallback")]
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "board"}


@pytest.fixture
def me(tg) -> tuple[Client, Any, Any]:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    client = Client()
    client.force_login(user)
    return client, user, make_kit_job(external_id="tv")


# --- the board ----------------------------------------------------------------------------------------------


def test_the_board_has_a_column_per_stage_and_a_dropdown_per_card(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job, "applied")
    html = client.get("/app/tracker/").content.decode()
    for label in (
        "Saved",
        "Applied",
        "Screening",
        "Interview",
        "Offer",
        "Hired",
        "Rejected",
        "Withdrawn",
        "Ghosted",
    ):
        assert f">{label} <span" in html
    assert html.count('class="stage-list') == 9
    assert f'data-stage-url="/app/tracker/{application.pk}/stage/"' in html
    assert f'id="stage-{application.pk}"' in html and '<option value="offer"' in html
    assert "Sortable.min.js" in html and "tracker.js" in html


def test_an_empty_board_explains_how_to_start(me) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = me
    assert "Nothing here yet" in client.get("/app/tracker/").content.decode()


def test_the_nav_links_to_the_tracker(me) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = me
    html = client.get("/app/").content.decode()
    assert 'href="/app/tracker/"' in html and "soon" not in html


def test_a_drop_changes_the_stage_records_an_event_and_returns_the_board(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job, "applied")
    response = client.post(f"/app/tracker/{application.pk}/stage/", {"stage": "interview"}, **HX)
    html = response.content.decode()
    application.refresh_from_db()
    assert application.stage == "interview"
    assert application.events.order_by("-id").first().to_stage == "interview"
    assert 'id="board"' in html and "<html" not in html  # just the board, to swap in


def test_the_mobile_dropdown_does_the_same(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job, "applied")
    client.post(f"/app/tracker/{application.pk}/stage/", {"stage": "offer"}, **HX)
    assert Application.objects.get(pk=application.pk).stage == "offer"
    assert ApplicationEvent.objects.filter(application=application, to_stage="offer").count() == 1


def test_a_plain_post_redirects_back(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job, "applied")
    response = client.post(
        f"/app/tracker/{application.pk}/stage/", {"stage": "screening", "next": "board"}
    )
    assert response["Location"] == "/app/tracker/"
    response = client.post(
        f"/app/tracker/{application.pk}/stage/", {"stage": "offer", "next": "//evil.example"}
    )
    assert response["Location"] == f"/app/tracker/{application.pk}/"  # never an open redirect


def test_a_bad_stage_changes_nothing(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job, "applied")
    client.post(f"/app/tracker/{application.pk}/stage/", {"stage": "nonsense"}, **HX)
    assert Application.objects.get(pk=application.pk).stage == "applied"
    assert application.events.count() == 1


def test_moving_needs_post_and_login(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job)
    assert client.get(f"/app/tracker/{application.pk}/stage/").status_code == 405
    assert "/accounts/login/" in Client().post(f"/app/tracker/{application.pk}/stage/")["Location"]


# --- applying from every entry point ----------------------------------------------------------------------------


def test_mark_applied_on_the_feed_creates_one_application(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    match = scored_match(user, score=80, job=job)
    for _ in range(2):  # pressing it twice must not create a second one
        client.post(f"/app/matches/{match.pk}/applied/", HTTP_HX_REQUEST="true")
    assert Application.objects.filter(user=user, job=job).count() == 1
    match.refresh_from_db()
    assert match.status == Match.Status.APPLIED
    assert Application.objects.get().events.count() == 1


def test_mark_applied_links_the_latest_kit(me, fake_llm) -> None:  # type: ignore[no-untyped-def]
    from tests.test_tracker_services import make_ready_kit

    client, user, job = me
    kit = make_ready_kit(user, job, fake_llm)
    match = scored_match(user, score=80, job=job)
    client.post(f"/app/matches/{match.pk}/applied/")
    assert Application.objects.get().kit == kit


def test_mark_applied_on_a_job_page_without_a_match(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert f'action="/app/tracker/apply/job/{job.pk}/"' in html
    response = client.post(f"/app/tracker/apply/job/{job.pk}/")
    application = Application.objects.get(user=user, job=job)
    assert response["Location"] == f"/app/tracker/{application.pk}/"
    client.post(f"/app/tracker/apply/job/{job.pk}/")
    assert Application.objects.count() == 1
    assert "In your tracker" in client.get(f"/app/jobs/{job.pk}/").content.decode()


def test_the_job_page_links_to_the_application_once_applied(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    match = scored_match(user, score=80, job=job)
    client.post(f"/app/matches/{match.pk}/applied/")
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "In your tracker" in html and "<strong>Applied</strong>" in html


def test_the_telegram_applied_button_creates_the_application(me, tg) -> None:  # type: ignore[no-untyped-def]
    _, user, job = me
    match = scored_match(user, score=92, job=job)
    notification_services.deliver(notification_services.request_instant(match)[0].pk)
    data = next(b.data for b in tg.messages[0]["buttons"][0] if b.text == "Applied ✓")
    bot_services.handle_callback(CHAT, "cb", data)
    bot_services.handle_callback(CHAT, "cb2", data)  # tapped twice
    application = Application.objects.get(user=user)
    assert application.job == job and application.events.count() == 1
    match.refresh_from_db()
    assert match.status == Match.Status.APPLIED


def test_all_three_entry_points_together_make_exactly_one(me, tg) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    match = scored_match(user, score=92, job=job)
    notification_services.deliver(notification_services.request_instant(match)[0].pk)
    data = next(b.data for b in tg.messages[0]["buttons"][0] if b.text == "Applied ✓")
    bot_services.handle_callback(CHAT, "cb", data)
    client.post(f"/app/matches/{match.pk}/applied/")
    client.post(f"/app/tracker/apply/job/{job.pk}/")
    assert Application.objects.count() == 1


def test_status_in_telegram_counts_applications_by_stage(me, tg) -> None:  # type: ignore[no-untyped-def]
    _, user, job = me
    seed(user, job, "applied")
    seed(user, make_kit_job(external_id="s2"), "interview")
    bot_services.handle_text(CHAT, "jane", "/status")
    assert "Applications: 1 applied, 1 interview" in tg.messages[-1]["text"]


# --- manual applications -------------------------------------------------------------------------------------------


def test_the_manual_form_creates_an_application_for_a_private_job(me) -> None:  # type: ignore[no-untyped-def]
    client, user, _ = me
    assert "Date you applied" in client.get("/app/tracker/add/").content.decode()
    response = client.post(
        "/app/tracker/add/",
        {"title": "Support Lead", "company": "Acme", "url": "https://acme.example/j", "applied_on": "2026-02-20",
         "channel": "Referral", "notes": "Sam said hi"},
    )  # fmt: skip
    application = Application.objects.get(user=user)
    assert response["Location"] == f"/app/tracker/{application.pk}/"
    assert application.job.visibility == "private" and application.job.owner == user
    assert application.applied_at == utc(2026, 2, 20, 12, 0)


@pytest.mark.parametrize(
    "bad",
    [
        {"title": "", "company": "A", "applied_on": "2026-02-20"},
        {"title": "T", "company": "", "applied_on": "2026-02-20"},
        {"title": "T", "company": "A", "applied_on": ""},
        {"title": "T", "company": "A", "applied_on": "not a date"},
        {"title": "T", "company": "A", "applied_on": "2999-01-01"},
        {"title": "T", "company": "A", "applied_on": "2026-02-20", "url": "javascript:alert(1)"},
    ],
)
def test_the_manual_form_rejects_bad_input(me, bad: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = me
    response = client.post("/app/tracker/add/", bad)
    assert response.status_code == 200 and response.context["form"].errors
    assert not Application.objects.exists()


# --- detail page ---------------------------------------------------------------------------------------------------


def test_the_detail_page_shows_job_stage_kit_notes_interviews_and_timeline(me, fake_llm) -> None:  # type: ignore[no-untyped-def]
    from tests.test_tracker_services import make_ready_kit

    client, user, job = me
    make_ready_kit(user, job, fake_llm)
    application, _ = services.apply_to_job(user, job, applied_at=NOW)
    services.change_stage(
        user, application, "screening", "Recruiter emailed", now=NOW + datetime.timedelta(days=2)
    )
    services.add_interview(
        user, application, kind="phone", scheduled_at=utc(2026, 3, 10, 9, 0), outcome="Passed"
    )
    html = client.get(f"/app/tracker/{application.pk}/").content.decode()
    assert job.title in html and job.company in html and "Screening" in html
    assert f"/app/kits/{application.kit.pk}/download/cv_pdf/" in html
    assert "Recruiter emailed" in html and "Applied &rarr; Screening" in html
    assert "Phone" in html and "Passed" in html
    assert "10 Mar 2026, 12:00" in html  # 09:00 UTC shown in Nairobi time
    assert 'name="notes"' in html and "Add an interview" in html


def test_the_ghosted_hint_appears_after_three_quiet_weeks(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    old = seed(user, job, applied=utc(2020, 1, 1))
    assert (
        "marking this application as ghosted"
        in client.get(f"/app/tracker/{old.pk}/").content.decode()
    )
    fresh = seed(
        user, make_kit_job(external_id="fresh"), applied=datetime.datetime.now(datetime.UTC)
    )
    assert (
        "marking this application as ghosted"
        not in client.get(f"/app/tracker/{fresh.pk}/").content.decode()
    )


def test_saving_notes_and_details(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job)
    response = client.post(
        f"/app/tracker/{application.pk}/details/",
        {
            "notes": "Call on Friday",
            "channel": "Email",
            "outcome_reason": "",
            "salary_offered": "$800",
        },
    )
    assert response.status_code == 302
    application.refresh_from_db()
    assert (application.notes, application.channel, application.salary_offered) == (
        "Call on Friday",
        "Email",
        "$800",
    )


def test_choosing_a_kit_version(me, fake_llm) -> None:  # type: ignore[no-untyped-def]
    from tests.test_tracker_services import make_ready_kit

    client, user, job = me
    kit = make_ready_kit(user, job, fake_llm)
    application = seed(user, job)
    client.post(f"/app/tracker/{application.pk}/kit/", {"kit": kit.pk})
    assert Application.objects.get(pk=application.pk).kit == kit
    client.post(f"/app/tracker/{application.pk}/kit/", {"kit": ""})
    assert Application.objects.get(pk=application.pk).kit is None
    response = client.post(f"/app/tracker/{application.pk}/kit/", {"kit": "999"}, follow=True)
    assert "not available for this job" in response.content.decode()


def test_interviews_are_typed_in_the_users_time_zone_and_stored_in_utc(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job)
    client.post(
        f"/app/tracker/{application.pk}/interviews/add/",
        {"scheduled_at": "2026-03-10T15:00", "kind": "technical", "notes": "Bring laptop"},
    )
    interview = Interview.objects.get(application=application)
    assert interview.scheduled_at == utc(2026, 3, 10, 12, 0) and interview.kind == "technical"
    html = client.get(f"/app/tracker/{application.pk}/").content.decode()
    assert 'value="2026-03-10T15:00"' in html  # the edit form shows the local time again


def test_interviews_can_be_edited_and_removed(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job)
    interview = services.add_interview(
        user, application, kind="phone", scheduled_at=utc(2026, 3, 10, 9, 0)
    )
    prefix = f"i{interview.pk}"
    client.post(
        f"/app/tracker/interviews/{interview.pk}/edit/",
        {f"{prefix}-scheduled_at": "2026-03-11T10:30", f"{prefix}-kind": "final", f"{prefix}-outcome": "Offer next",
         f"{prefix}-notes": "", f"{prefix}-questions_asked": "Why us?"},
    )  # fmt: skip
    interview.refresh_from_db()
    assert (interview.kind, interview.outcome, interview.scheduled_at) == (
        "final",
        "Offer next",
        utc(2026, 3, 11, 7, 30),
    )
    client.post(f"/app/tracker/interviews/{interview.pk}/delete/")
    assert not Interview.objects.exists()


def test_a_bad_interview_date_is_refused(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job)
    response = client.post(
        f"/app/tracker/{application.pk}/interviews/add/",
        {"scheduled_at": "tomorrow", "kind": "phone"},
    )
    assert response.status_code == 400 and not Interview.objects.exists()


# --- stats page ---------------------------------------------------------------------------------------------------------


def test_the_stats_page_shows_rates_and_tables(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    application = seed(user, job, "screening")
    ApplicationEvent.objects.create(
        user=user,
        application=application,
        from_stage="applied",
        to_stage="screening",
        at=NOW + datetime.timedelta(days=3),
    )
    seed(user, make_kit_job(external_id="st2"), "ghosted")
    html = client.get("/app/tracker/stats/").content.decode()
    assert "50%" in html and "1 of 2 got an answer" in html and ">3.0<" in html
    assert (
        "By where you found it" in html and "With and without a kit" in html and "Ghosted" in html
    )


def test_the_stats_page_works_with_no_data(me) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = me
    response = client.get("/app/tracker/stats/")
    assert response.status_code == 200 and "Nothing to show yet" in response.content.decode()


# --- isolation --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def two(tg) -> tuple[Client, Any, Any, Application, Interview]:  # type: ignore[no-untyped-def]
    a = make_notif_user(chat_id=None)
    b = make_notif_user(chat_id="2", full_name="Bob Buffalo")
    application = seed(b, make_kit_job(external_id="bobs", title="Bobs Secret Job"), "applied")
    interview = services.add_interview(b, application, kind="phone", notes="secret")
    client = Client()
    client.force_login(a)
    return client, a, b, application, interview


@pytest.mark.parametrize("path", ["", "stage/", "details/", "kit/", "interviews/add/"])
def test_another_users_application_urls_are_404(two, path: str) -> None:  # type: ignore[no-untyped-def]
    client, _, _, application, _ = two
    url = f"/app/tracker/{application.pk}/{path}"
    response = (
        client.get(url)
        if path == ""
        else client.post(url, {"stage": "offer", "notes": "x", "kit": "", "kind": "phone"})
    )
    assert response.status_code == 404
    application.refresh_from_db()
    assert (
        application.stage == "applied"
        and application.notes == ""
        and application.events.count() == 1
    )


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_another_users_interviews_are_404(two, action: str) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _, interview = two
    response = client.post(f"/app/tracker/interviews/{interview.pk}/{action}/", {"kind": "final"})
    assert response.status_code == 404
    interview.refresh_from_db()
    assert interview.kind == "phone" and interview.notes == "secret"


def test_the_board_and_stats_never_show_other_users_applications(two) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _, _ = two
    assert "Bobs Secret Job" not in client.get("/app/tracker/").content.decode()
    assert "1 of" not in client.get("/app/tracker/stats/").content.decode()
    assert "Bobs Secret Job" not in client.get("/app/").content.decode()


def test_applying_to_someone_elses_private_job_is_404(two) -> None:  # type: ignore[no-untyped-def]
    client, _, b, _, _ = two
    private = make_kit_job(visibility="private", owner=b, source=None, external_id="priv")
    assert client.post(f"/app/tracker/apply/job/{private.pk}/").status_code == 404
    assert not Application.objects.filter(job=private).exists()


def test_a_match_applied_url_for_another_users_match_is_404(two) -> None:  # type: ignore[no-untyped-def]
    client, _, b, _, _ = two
    theirs = MatchFactory(user=b, job=make_kit_job(external_id="bm"))
    assert client.post(f"/app/matches/{theirs.pk}/applied/").status_code == 404
    assert Application.objects.count() == 1  # still only Bob's


def test_for_user_scopes_every_tracker_model(two) -> None:  # type: ignore[no-untyped-def]
    _, a, b, _, _ = two
    for model in (Application, ApplicationEvent, Interview):
        assert model.objects.for_user(a).count() == 0 and model.objects.for_user(b).count() == 1
        assert model.objects.for_user(None).count() == 0


def test_the_admin_shows_no_notes(two, admin_client) -> None:  # type: ignore[no-untyped-def]
    _, _, b, application, _ = two
    Application.objects.filter(pk=application.pk).update(notes="very private note")
    page = admin_client.get(f"/admin/tracker/application/{application.pk}/change/").content.decode()
    assert "very private note" not in page and "notes" not in page.split("<form")[1]
    assert UserFactory and b


def test_the_board_stacks_on_phones_and_runs_sideways_on_larger_screens(me) -> None:  # type: ignore[no-untyped-def]
    client, user, job = me
    seed(user, job)
    html = client.get("/app/tracker/").content.decode()
    assert "{#" not in html and "{%" not in html  # no template comment leaks into the page
    assert "flex flex-col" in html and "md:flex-row" in html  # vertical by default, row from md
    assert "md:overflow-x-auto" in html and 'class="mt-4 overflow-x-auto' not in html
    script = (__import__("pathlib").Path("static/js/tracker.js")).read_text(encoding="utf-8")
    assert "delayOnTouchOnly: true" in script  # press-and-hold on touch, instant with a mouse
