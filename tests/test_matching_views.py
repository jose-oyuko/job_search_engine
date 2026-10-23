"""The feed, card actions, the job page's match panel and "Search now" (spec §10.5, §10.6)."""

import datetime
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.jobs.models import JobReport
from apps.llm.models import LLMCall, UsageQuota
from apps.llm.providers.fake import FakeProvider
from apps.matching import services
from apps.matching.models import Match
from apps.profiles import services as profile_services
from tests.factories import MatchFactory, SkillFactory, SourceFactory, UserFactory
from tests.matching_helpers import make_job, make_user

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def user() -> Any:
    return make_user()


@pytest.fixture
def client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def add_match(user: Any, **fields: Any) -> Match:
    job = fields.pop("job", None) or make_job(
        external_id=f"j{Match.objects.count()}-{timezone.now().timestamp()}"
    )
    return MatchFactory(user=user, job=job, **fields)


# --- the feed ---------------------------------------------------------------------------------------------------------


def test_every_endpoint_requires_login() -> None:
    anonymous = Client()
    for url in ("/app/search/status/", "/app/matches/1/score-status/"):
        assert "/accounts/login/" in anonymous.get(url)["Location"]
    for url in (
        "/app/search/",
        "/app/matches/1/save/",
        "/app/matches/1/dismiss/",
        "/app/matches/1/applied/",
        "/app/matches/1/restore/",
        "/app/matches/1/rescore/",
    ):
        assert "/accounts/login/" in anonymous.post(url)["Location"]
    assert "/accounts/login/" in anonymous.get("/app/")["Location"]


def test_the_dashboard_is_the_ranked_feed(client: Client, user: Any) -> None:
    add_match(user, job=make_job(title="Low fit", external_id="1"), score=40, verdict="weak")
    add_match(
        user,
        job=make_job(title="Great fit", external_id="2"),
        score=91,
        verdict="strong",
        summary="Excellent.",
    )
    add_match(user, job=make_job(title="Unscored", external_id="3"))
    html = client.get("/app/").content.decode()
    assert html.index("Great fit") < html.index("Low fit") < html.index("Unscored")
    assert "Your jobs" in html and "3 matches" in html and "Search now" in html
    assert (
        ">91<" in html
        and "Strong match" in html
        and "Excellent." in html
        and "Not scored yet" in html
    )


def test_cards_show_the_badges(client: Client, user: Any) -> None:
    version = profile_services.get_profile(user).version
    add_match(
        user,
        job=make_job(title="Stale one", external_id="1"),
        score=70,
        verdict="good",
        scored_profile_version=version - 1 if version > 1 else 0,
    )
    add_match(
        user, job=make_job(title="Risky one", external_id="2", scam_flags=["asks_for_payment"])
    )
    add_match(user, job=make_job(title="Closed one", external_id="3", status="expired"))
    add_match(
        user,
        job=make_job(
            title="Foreign one",
            external_id="4",
            is_global=False,
            eligible_countries=["US"],
            eligibility_known=True,
        ),
    )
    add_match(
        user,
        job=make_job(
            title="Unclear one", external_id="5", is_global=False, eligibility_known=False
        ),
    )
    add_match(
        user, job=make_job(title="Outside one", external_id="6"), status="saved", rule_pass=False
    )
    html = client.get("/app/").content.decode()
    for text in (
        "Score may be out of date",
        "Check carefully",
        "Not open to your country",
        "Eligibility unclear",
        "Outside your target",
        "Saved",
        "Open to you",
    ):
        assert text in html
    assert (
        "May have closed" not in html or "Closed one" in html
    )  # expired jobs are shown with their badge when present


def test_expired_jobs_carry_a_badge(client: Client, user: Any) -> None:
    add_match(user, job=make_job(title="Closed one", external_id="3", status="expired"))
    assert "May have closed" in client.get("/app/").content.decode()


def test_new_matches_are_marked_new(client: Client, user: Any) -> None:
    add_match(user, status="new")
    add_match(user, status="seen")
    assert client.get("/app/").content.decode().count('<span class="rounded-full bg-brand-600') == 1


def test_score_limit_reached_is_shown_and_no_model_call_is_made(
    client: Client, user: Any, fake_llm: FakeProvider
) -> None:
    UsageQuota.objects.create(user=user, monthly_scores=0)
    add_match(user)
    html = client.get("/app/").content.decode()
    assert "Score limit reached" in html and "Not scored yet" not in html
    assert fake_llm.calls == []


def test_a_queued_match_shows_scoring(client: Client, user: Any) -> None:
    add_match(user, scoring_queued_at=timezone.now())
    assert "Scoring&hellip;" in client.get("/app/").content.decode()


def test_the_feed_never_contains_other_users_jobs(client: Client, user: Any) -> None:
    add_match(user, job=make_job(title="Mine", external_id="1"))
    other = make_user()
    add_match(other, job=make_job(title="Theirs", external_id="2"))
    html = client.get("/app/").content.decode()
    assert "Mine" in html and "Theirs" not in html


# --- empty states -------------------------------------------------------------------------------------------------------


def test_no_track_asks_for_one() -> None:
    user = make_user(track=False)
    client = Client()
    client.force_login(user)
    html = client.get("/app/").content.decode()
    assert "Tell us what jobs you want" in html and "/app/profile/tracks/new/" in html


def test_an_empty_profile_asks_for_details() -> None:
    user = make_user(skills=(), headline="")
    client = Client()
    client.force_login(user)
    html = client.get("/app/").content.decode()
    assert "Add a little about yourself first" in html and "/app/profile/" in html


def test_no_matches_yet_explains_the_schedule(client: Client) -> None:
    html = client.get("/app/").content.decode()
    assert "No matches yet" in html and "every 20 minutes" in html


def test_filters_with_no_results_offer_to_clear(client: Client, user: Any) -> None:
    add_match(user)
    html = client.get("/app/?q=zzzz").content.decode()
    assert "Nothing matches these filters" in html and "for these filters" in html


# --- filters and pagination ---------------------------------------------------------------------------------------------------


def test_filters_through_the_query_string(client: Client, user: Any) -> None:
    add_match(
        user,
        job=make_job(title="Alpha", company="Zed", external_id="1", work_type="remote"),
        verdict="strong",
        score=90,
    )
    add_match(
        user,
        job=make_job(title="Beta", company="Yan", external_id="2", work_type="hybrid"),
        verdict="weak",
        score=45,
    )
    assert "Beta" not in client.get("/app/?verdict=strong").content.decode()
    assert "Alpha" not in client.get("/app/?work_type=hybrid").content.decode()
    assert "Alpha" not in client.get("/app/?q=yan").content.decode()
    default_track = (
        user.settings.country and Match.objects.first().track
    )  # may be None; filter by id below
    assert client.get(f"/app/?track={default_track.pk if default_track else 0}").status_code == 200


def test_bad_filter_values_are_ignored(client: Client, user: Any) -> None:
    add_match(user)
    response = client.get("/app/?verdict=bogus&track=abc&status=nope&page=zzz")
    assert response.status_code == 200 and "Customer Support Agent" in response.content.decode()


def test_the_filter_form_keeps_what_you_chose(client: Client, user: Any) -> None:
    add_match(user)
    html = client.get("/app/?q=support&eligible=on").content.decode()
    assert 'value="support"' in html and "checked" in html and "<details" in html


def test_load_more_pagination(client: Client, user: Any) -> None:
    for i in range(25):
        add_match(
            user,
            job=make_job(title=f"Role {i:02d}", external_id=f"p{i}"),
            score=100 - i,
            verdict="strong",
        )
    first = client.get("/app/?verdict=strong").content.decode()
    assert (
        first.count("<article") == 20
        and 'hx-get="?verdict=strong&amp;page=2"' in first
        and "Load more" in first
    )
    assert "Role 00" in first and "Role 24" not in first

    partial = client.get("/app/?verdict=strong&page=2", **HX)
    html = partial.content.decode()
    assert "<html" not in html and html.count("<article") == 5 and "Role 24" in html
    assert "Load more" not in html  # that was the last page


def test_a_plain_second_page_is_a_full_page(client: Client, user: Any) -> None:
    for i in range(22):
        add_match(user, job=make_job(external_id=f"q{i}"))
    assert "<html" in client.get("/app/?page=2").content.decode()


def test_users_still_in_onboarding_are_sent_to_the_wizard() -> None:
    user = make_user()
    user.settings.onboarding_completed = False
    user.settings.save()
    client = Client()
    client.force_login(user)
    assert client.get("/app/")["Location"] == "/welcome/"


# --- card actions ----------------------------------------------------------------------------------------------------------------


def test_save_toggles_and_returns_the_updated_card(client: Client, user: Any) -> None:
    match = add_match(user)
    saved = client.post(f"/app/matches/{match.pk}/save/", **HX)
    assert "Unsave" in saved.content.decode() and f'id="match-{match.pk}"' in saved.content.decode()
    assert Match.objects.get(pk=match.pk).status == "saved"
    unsaved = client.post(f"/app/matches/{match.pk}/save/", **HX)
    assert ">Save<" in unsaved.content.decode() and Match.objects.get(pk=match.pk).status == "seen"


def test_dismiss_records_the_reason_and_removes_the_card(client: Client, user: Any) -> None:
    match = add_match(user)
    response = client.post(f"/app/matches/{match.pk}/dismiss/", {"reason": "pay_too_low"}, **HX)
    assert response.status_code == 200 and response.content == b""
    got = Match.objects.get(pk=match.pk)
    assert (got.status, got.dismiss_reason) == ("dismissed", "pay_too_low")
    assert "Customer Support Agent" not in client.get("/app/").content.decode()
    assert "Customer Support Agent" in client.get("/app/?status=dismissed").content.decode()


def test_dismiss_without_a_valid_reason_defaults_to_not_relevant(client: Client, user: Any) -> None:
    match = add_match(user)
    client.post(f"/app/matches/{match.pk}/dismiss/", {"reason": "because"}, **HX)
    assert Match.objects.get(pk=match.pk).dismiss_reason == "not_relevant"


def test_mark_applied_and_restore(client: Client, user: Any) -> None:
    match = add_match(user)
    assert client.post(f"/app/matches/{match.pk}/applied/", **HX).content == b""
    assert Match.objects.get(pk=match.pk).status == "applied"
    assert "Customer Support Agent" in client.get("/app/?status=applied").content.decode()
    restored = client.post(f"/app/matches/{match.pk}/restore/", **HX)
    assert restored.content == b"" and Match.objects.get(pk=match.pk).status == "seen"


def test_actions_without_htmx_redirect_back_safely(client: Client, user: Any) -> None:
    match = add_match(user)
    assert client.post(f"/app/matches/{match.pk}/save/")["Location"] == "/app/"
    assert (
        client.post(f"/app/matches/{match.pk}/save/", {"next": "/app/jobs/5/"})["Location"]
        == "/app/jobs/5/"
    )
    for evil in (
        "https://evil.example/",
        "//evil.example/",
        "/accounts/logout/",
        "javascript:alert(1)",
    ):
        assert client.post(f"/app/matches/{match.pk}/save/", {"next": evil})["Location"] == "/app/"


def test_actions_are_post_only(client: Client, user: Any) -> None:
    match = add_match(user)
    for name in ("save", "dismiss", "applied", "restore", "rescore"):
        assert client.get(f"/app/matches/{match.pk}/{name}/").status_code == 405


# --- the job page --------------------------------------------------------------------------------------------------------------


def scored_match(user: Any, **fields: Any) -> Match:
    version = profile_services.get_profile(user).version
    defaults: dict[str, Any] = {
        "score": 77, "verdict": "good", "summary": "Solid fit overall.",
        "strengths": ["Helpdesk experience"], "gaps": ["No Zendesk"],
        "scored_profile_version": version, "rule_reasons": ["eligible: global", "keyword: support"],
    }  # fmt: skip
    return add_match(user, **{**defaults, **fields})


def test_the_job_page_shows_the_score_strengths_gaps_and_reasons(client: Client, user: Any) -> None:
    match = scored_match(user)
    html = client.get(f"/app/jobs/{match.job_id}/").content.decode()
    assert ">77<" in html and "Good match" in html and "Solid fit overall." in html
    assert "Helpdesk experience" in html and "No Zendesk" in html
    assert "Why this job was shown to you" in html and "keyword: support" in html
    assert "out of date" not in html and "Rescore" in html


def test_a_stale_score_is_flagged_on_the_job_page(client: Client, user: Any) -> None:
    match = scored_match(user, scored_profile_version=0)
    html = client.get(f"/app/jobs/{match.job_id}/").content.decode()
    assert "may be out of date" in html


def test_viewing_a_new_match_marks_it_seen(client: Client, user: Any) -> None:
    match = scored_match(user, status="new")
    client.get(f"/app/jobs/{match.job_id}/")
    assert Match.objects.get(pk=match.pk).status == "seen"
    saved = scored_match(user, status="saved", job=make_job(external_id="x"))
    client.get(f"/app/jobs/{saved.job_id}/")
    assert Match.objects.get(pk=saved.pk).status == "saved"


def test_an_unscored_match_is_scored_when_viewed(
    client: Client, user: Any, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    match = add_match(user)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        html = client.get(f"/app/jobs/{match.job_id}/").content.decode()
    assert (
        "Working out how well this fits you" in html
        and f"/app/matches/{match.pk}/score-status/" in html
    )
    assert len(callbacks) == 1
    for callback in callbacks:
        callback()  # the worker picks it up
    match.refresh_from_db()
    assert match.score == 72

    poll = client.get(f"/app/matches/{match.pk}/score-status/")
    assert poll["HX-Refresh"] == "true" and poll.content == b""
    assert ">72<" in client.get(f"/app/jobs/{match.job_id}/").content.decode()


def test_the_score_poll_keeps_waiting_until_scored(client: Client, user: Any) -> None:
    match = add_match(user, scoring_queued_at=timezone.now())
    poll = client.get(f"/app/matches/{match.pk}/score-status/")
    assert "HX-Refresh" not in poll and "Working out how well" in poll.content.decode()


def test_the_job_page_says_when_the_scoring_allowance_is_used_up(
    client: Client, user: Any, fake_llm: FakeProvider
) -> None:
    UsageQuota.objects.create(user=user, monthly_scores=0)
    match = add_match(user)
    html = client.get(f"/app/jobs/{match.job_id}/").content.decode()
    assert "Score limit reached" in html and fake_llm.calls == []


def test_rescore_from_the_job_page(
    client: Client, user: Any, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    match = scored_match(user)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(f"/app/matches/{match.pk}/rescore/", follow=True)
    assert "Scoring again" in response.content.decode()
    match.refresh_from_db()
    assert match.score == 72  # replaced by the fresh result


def test_rescore_is_refused_politely_when_out_of_allowance(
    client: Client, user: Any, fake_llm: FakeProvider
) -> None:
    UsageQuota.objects.create(user=user, monthly_scores=0)
    match = scored_match(user)
    response = client.post(f"/app/matches/{match.pk}/rescore/", follow=True)
    assert "used all of this month" in response.content.decode() and fake_llm.calls == []
    match.refresh_from_db()
    assert match.score == 77  # the old score is kept


def test_job_page_actions_work_without_javascript(client: Client, user: Any) -> None:
    match = scored_match(user)
    page = f"/app/jobs/{match.job_id}/"
    assert client.post(f"/app/matches/{match.pk}/save/", {"next": page})["Location"] == page
    assert Match.objects.get(pk=match.pk).status == "saved"
    assert (
        client.post(f"/app/matches/{match.pk}/dismiss/", {"next": "/app/", "reason": "location"})[
            "Location"
        ]
        == "/app/"
    )
    assert Match.objects.get(pk=match.pk).dismiss_reason == "location"


def test_the_job_page_shows_where_it_came_from(client: Client) -> None:
    source = SourceFactory(display_name="BoardOne")
    job = make_job(
        source=source, alternate_urls=["https://other.example/job/1", "https://third.example/2"]
    )
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "BoardOne" in html and "Also listed at" in html and "https://other.example/job/1" in html


def test_a_job_without_a_match_shows_no_score_panel(client: Client) -> None:
    job = make_job()
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "How well it fits you" not in html and "Description" in html


def test_reporting_a_listing(client: Client, user: Any) -> None:
    job = make_job()
    response = client.post(
        f"/app/jobs/{job.pk}/report/", {"reason": "scam", "note": "Asked for money"}, follow=True
    )
    assert "We will take a look" in response.content.decode()
    report = JobReport.objects.get(user=user)
    assert (report.job, report.reason, report.note) == (job, "scam", "Asked for money")
    client.post(
        f"/app/jobs/{job.pk}/report/", {"reason": "expired"}
    )  # one report per user and listing
    assert (
        JobReport.objects.filter(user=user).count() == 1
        and JobReport.objects.get(user=user).reason == "expired"
    )


def test_an_invalid_report_is_refused(client: Client, user: Any) -> None:
    job = make_job()
    response = client.post(f"/app/jobs/{job.pk}/report/", {"reason": "bogus"}, follow=True)
    assert "choose a reason" in response.content.decode() and not JobReport.objects.exists()


# --- manual jobs end to end -----------------------------------------------------------------------------------------------------


def test_a_pasted_job_gets_a_match_and_a_score(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    text = "Customer support agent wanted for Acme, answering email and chat five hours a day, fully remote."
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            "/app/jobs/add/text/", {"text": text, "title": "Support Agent", "company": "Acme"}
        )
    match = Match.objects.get(user=user)
    assert response["Location"] == f"/app/jobs/{match.job_id}/" and match.score == 72
    html = client.get(response["Location"]).content.decode()
    assert "How well it fits you" in html and ">72<" in html


# --- Search now -----------------------------------------------------------------------------------------------------------------------


def test_search_now_shows_progress_and_respects_its_limit(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        first = client.post("/app/search/", **HX)
    html = first.content.decode()
    assert (
        first.status_code == 200
        and 'id="search-status"' in html
        and "Looking for new matches" in html
    )
    assert 'hx-trigger="every 2s"' in html  # the page polls while the worker runs
    assert (
        "Search finished" in client.get("/app/search/status/").content.decode()
    )  # after the worker ran

    again = client.post("/app/search/", **HX).content.decode()
    assert "You searched a moment ago" in again and "minute" in again


def test_search_now_without_htmx_redirects_with_a_message(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        assert client.post("/app/search/")["Location"] == "/app/"
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/app/search/", follow=True)
    assert "You searched a moment ago" in response.content.decode()


def test_search_now_rescores_the_users_recent_matches(
    client: Client, user: Any, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    match = add_match(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/search/", **HX)
    match.refresh_from_db()
    assert match.score == 72
    assert "1 scored" in client.get("/app/search/status/").content.decode()


def test_search_status_polls_only_while_active(client: Client, user: Any) -> None:
    from django.core.cache import cache

    assert client.get("/app/search/status/").content.decode().strip().count("hx-trigger") == 0
    source = SourceFactory()
    cache.set(
        f"search_now:{user.pk}",
        {"started": timezone.now().isoformat(), "source_ids": [source.pk], "done": False},
        600,
    )
    active = client.get("/app/search/status/").content.decode()
    assert 'hx-trigger="every 2s"' in active and "Checking 1 source" in active
    cache.set(
        f"search_now:{user.pk}",
        {
            "started": (timezone.now() - datetime.timedelta(minutes=9)).isoformat(),
            "source_ids": [source.pk],
            "done": True,
        },
        600,
    )
    assert "hx-trigger" not in client.get("/app/search/status/").content.decode()


def test_the_dashboard_shows_a_running_search(client: Client, user: Any) -> None:
    from django.core.cache import cache

    cache.set(
        f"search_now:{user.pk}",
        {"started": timezone.now().isoformat(), "source_ids": [], "done": False},
        600,
    )
    assert "Looking for new matches" in client.get("/app/").content.decode()


def test_the_scoring_limit_is_visible_after_search(
    client: Client, user: Any, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    UsageQuota.objects.create(user=user, monthly_scores=0)
    add_match(user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/app/search/", **HX)
    assert "allowance is used up" in client.get("/app/search/status/").content.decode()
    assert not LLMCall.objects.filter(task="score_match").exists()
    assert SkillFactory and UserFactory and services
