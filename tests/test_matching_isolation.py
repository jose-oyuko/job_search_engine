"""Multi-user isolation for matching (spec §16): user A can never see or act on user B's matches,
private jobs, scores or search progress."""

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client

from apps.jobs import manual
from apps.jobs.models import Job, JobReport
from apps.llm.models import LLMCall, UsageQuota
from apps.llm.providers.fake import FakeProvider
from apps.matching import services
from apps.matching.models import Match
from tests.factories import MatchFactory
from tests.matching_helpers import make_job, make_user

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def a_and_b() -> tuple[Client, Any, Any]:
    a, b = make_user(), make_user()
    client = Client()
    client.force_login(a)
    return client, a, b


def b_match(b: Any, **fields: Any) -> Match:
    return MatchFactory(user=b, job=fields.pop("job", None) or make_job(external_id="bj"), **fields)


@pytest.mark.parametrize("action", ["save", "dismiss", "applied", "restore", "rescore"])
def test_other_users_matches_cannot_be_acted_on(a_and_b, action: str) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    match = b_match(b, status="new", score=88, verdict="strong")
    before = Match.objects.get(pk=match.pk)
    response = client.post(f"/app/matches/{match.pk}/{action}/", {"reason": "other"}, **HX)
    assert response.status_code == 404
    after = Match.objects.get(pk=match.pk)
    assert (after.status, after.score, after.dismiss_reason) == (
        before.status,
        before.score,
        before.dismiss_reason,
    )
    assert after.scoring_queued_at is None


def test_other_users_score_polls_are_not_visible(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    match = b_match(b)
    assert client.get(f"/app/matches/{match.pk}/score-status/").status_code == 404


def test_the_feed_and_every_filter_exclude_other_users_matches(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    b_match(
        b,
        job=make_job(title="Secret B Role", external_id="s1"),
        score=99,
        verdict="strong",
        status="saved",
    )
    MatchFactory(
        user=a, job=make_job(title="Visible A Role", external_id="s2"), score=10, verdict="poor"
    )
    for query in (
        "",
        "?verdict=strong",
        "?status=saved",
        "?q=secret",
        "?eligible=on",
        "?new=on",
        "?work_type=remote",
    ):
        html = client.get(f"/app/{query}").content.decode()
        assert "Secret B Role" not in html
    assert "Visible A Role" in client.get("/app/").content.decode()
    assert Match.objects.for_user(a).count() == 1


def test_a_match_on_a_public_job_is_not_exposed_through_the_job_page(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, _, b = a_and_b
    job = make_job()
    b_match(
        b,
        job=job,
        score=95,
        verdict="strong",
        summary="B's private summary",
        strengths=["B strength"],
    )
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert (
        "B's private summary" not in html
        and "B strength" not in html
        and "How well it fits you" not in html
    )
    assert "95" not in html.split("Description")[0].split("<h1")[1]


def test_private_jobs_are_404_for_everyone_but_their_owner(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    private = make_job(
        visibility="private", owner=b, source=None, origin="manual_text", title="B only"
    )
    assert client.get(f"/app/jobs/{private.pk}/").status_code == 404
    assert client.post(f"/app/jobs/{private.pk}/report/", {"reason": "scam"}).status_code == 404
    assert not JobReport.objects.exists()
    owner = Client()
    owner.force_login(b)
    assert owner.get(f"/app/jobs/{private.pk}/").status_code == 200
    assert a


def test_a_private_job_never_reaches_other_users_through_matching(a_and_b) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    private = make_job(
        visibility="private", owner=b, source=None, origin="manual_text", title="Support B private"
    )
    assert services.match_new_job(private.pk) == []
    services.rematch_user(a.pk)
    assert not Match.objects.filter(user=a, job=private).exists()
    assert "Support B private" not in client.get("/app/").content.decode()


def test_manual_text_jobs_stay_private_and_score_per_user(
    a_and_b, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, a, b = a_and_b
    text = "Customer support agent wanted, answering email and chat five hours a day, fully remote, Acme."
    with django_capture_on_commit_callbacks(execute=True):
        manual.add_job_from_text(b, text, title="Support Agent", company="Acme")
    job = Job.objects.get(owner=b)
    assert (
        Match.objects.filter(user=b, job=job).exists() and not Match.objects.filter(user=a).exists()
    )
    assert LLMCall.objects.filter(user=a).count() == 0  # A was never charged for B's job
    assert all(call.user in (None, b) for call in LLMCall.objects.all())


def test_scoring_cache_keys_are_per_user(a_and_b, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    _, a, b = a_and_b
    job = make_job()
    ctx_a, ctx_b = services.build_context(a), services.build_context(b)
    match_a = services.upsert_match(ctx_a, job)[0]  # type: ignore[arg-type]
    match_b = services.upsert_match(ctx_b, job)[0]  # type: ignore[arg-type]
    services.score_match(match_a.pk)  # type: ignore[union-attr]
    services.score_match(match_b.pk)  # type: ignore[union-attr]
    assert len(fake_llm.calls) == 2  # identical inputs, yet each user's score is their own


def test_scoring_one_users_match_does_not_use_anothers_allowance(
    a_and_b, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    _, a, b = a_and_b
    UsageQuota.objects.create(user=a, monthly_scores=0)
    match_b = services.upsert_match(services.build_context(b), make_job())[0]  # type: ignore[arg-type]
    assert services.scores_exhausted(a) and not services.scores_exhausted(b)
    assert services.score_match(match_b.pk) == services.SCORED  # type: ignore[union-attr]


def test_rematching_one_user_leaves_the_other_untouched(a_and_b) -> None:  # type: ignore[no-untyped-def]
    _, a, b = a_and_b
    job = make_job()
    services.rematch_user(a.pk)
    services.rematch_user(b.pk)
    Match.objects.filter(user=b).update(status="saved", score=77)
    from apps.profiles.models import Track

    Track.objects.filter(user=a).update(exclude_keywords=["customer"])
    services.rematch_user(a.pk)
    assert not Match.objects.filter(user=a, job=job).exists()
    kept = Match.objects.get(user=b, job=job)
    assert (kept.status, kept.score) == ("saved", 77)


def test_search_progress_and_the_cooldown_are_per_user(
    a_and_b, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    _, a, b = a_and_b
    with django_capture_on_commit_callbacks(execute=False):
        services.start_search(a)
        assert services.search_progress(b) is None  # B sees nothing of A's search
        services.start_search(b)  # and is not held back by A's cooldown
        with pytest.raises(services.SearchTooSoon):
            services.start_search(a)


def test_for_user_scopes_matches_and_reports_for_everyone() -> None:
    a, b = make_user(), make_user()
    MatchFactory(user=a, job=make_job(external_id="1"))
    MatchFactory(user=b, job=make_job(external_id="2"))
    JobReport.objects.create(user=a, job=make_job(external_id="3"), reason="scam")
    assert Match.objects.for_user(a).count() == 1 and Match.objects.for_user(a).get().user == a
    assert (
        Match.objects.for_user(AnonymousUser()).count() == 0
        and Match.objects.for_user(None).count() == 0
    )
    assert JobReport.objects.for_user(b).count() == 0


def test_the_service_layer_refuses_foreign_matches(a_and_b) -> None:  # type: ignore[no-untyped-def]
    from apps.matching.exceptions import NotOwner

    _, a, b = a_and_b
    match = b_match(b)
    for call in (
        lambda: services.set_status(a, match, "saved"),
        lambda: services.request_rescore(a, match),
        lambda: services.ensure_scoring_queued(a, match, 1),
    ):
        with pytest.raises(NotOwner):
            call()
