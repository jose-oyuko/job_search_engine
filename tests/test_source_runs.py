"""run_source integration (with HTTP mocked), health transitions, the scheduler, re-checks and the
management commands."""

import datetime
import json
from io import StringIO

import httpx
import pytest
import respx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.core.tasks import enqueue
from apps.jobs.models import Job
from apps.jobs.tasks import expire_stale
from apps.llm.models import LLMCall
from apps.sources import http, services
from apps.sources.models import Source, SourceRun
from apps.sources.tasks import run_source_task, tick
from tests.factories import JobFactory, SourceFactory
from tests.logcapture import ListHandler
from tests.source_helpers import (
    DETAIL_FIXTURES,
    LISTING_URL,
    mock_details,
    mock_listing,
)

pytestmark = pytest.mark.django_db

FIVE = list(DETAIL_FIXTURES)  # the five listing IDs we have saved detail pages for


def listing_of(ids: list[str]) -> str:
    return "".join(f'<a href="https://expertwoka.com/opportunities/{i}">{i}</a>' for i in ids)


@pytest.fixture
def ew() -> Source:
    services.seed_sources()
    return Source.objects.get(name="expertwoka")


def go(source: Source, **kwargs: object) -> SourceRun:
    with http.build_client() as client:
        run = services.run_source(source, client=client, sleep=lambda s: None, **kwargs)  # type: ignore[arg-type]
    assert run is not None
    return run


# --- a full ExpertWoka run ---------------------------------------------------------------------------------


def test_a_run_ingests_new_jobs_and_records_the_counts(
    ew: Source, router: respx.MockRouter
) -> None:
    mock_listing(router, listing_of(FIVE))
    mock_details(router, only=FIVE)

    run = go(ew)

    ew.refresh_from_db()
    assert run.success and run.finished_at and run.errors == []
    assert (run.ids_seen, run.new_jobs, run.updated_jobs) == (5, 5, 0)
    assert Job.objects.filter(source=ew).count() == 5
    assert (ew.consecutive_failures, ew.health) == (0, "ok")
    assert ew.last_run_at and ew.last_success_at
    job = Job.objects.get(external_id="finance-expert-afterquery")
    assert (job.title, job.company, job.pay_max, job.pay_period) == (
        "Finance Expert",
        "AfterQuery",
        200,
        "hour",
    )
    assert job.eligible_countries == ["US"] and job.work_type == "remote"


def test_rerun_creates_no_duplicates_and_fetches_no_details(
    ew: Source, router: respx.MockRouter
) -> None:
    listing = mock_listing(router, listing_of(FIVE))
    routes = mock_details(router, only=FIVE)
    go(ew)
    detail_calls = sum(r.call_count for r in routes.values())
    assert detail_calls == 5

    second = go(ew)

    assert (second.ids_seen, second.new_jobs) == (5, 0)
    assert sum(r.call_count for r in routes.values()) == detail_calls  # no new detail requests
    assert listing.call_count == 2 and Job.objects.count() == 5


def test_only_newly_listed_ids_trigger_detail_fetches(ew: Source, router: respx.MockRouter) -> None:
    mock_listing(router, listing_of(FIVE[:3]))
    routes = mock_details(router, only=FIVE)
    go(ew)
    first_total = sum(r.call_count for r in routes.values())

    mock_listing(router, listing_of(FIVE))  # two more appear
    third = go(ew)

    assert first_total == 3 and third.new_jobs == 2 and third.ids_seen == 5
    assert sum(r.call_count for r in routes.values()) == 5


def test_jobs_still_listed_stay_fresh_without_being_refetched(
    ew: Source, router: respx.MockRouter
) -> None:
    mock_listing(router, listing_of(FIVE))
    mock_details(router, only=FIVE)
    go(ew)
    old = timezone.now() - datetime.timedelta(days=5)
    Job.objects.update(last_seen_at=old)
    go(ew)
    assert not Job.objects.filter(last_seen_at__lte=old).exists()


def test_new_jobs_are_extracted_by_the_llm_after_the_run(
    ew: Source, router: respx.MockRouter, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router, listing_of(FIVE[:2]))
    mock_details(router, only=FIVE)
    with django_capture_on_commit_callbacks(execute=True):
        go(ew)
    assert Job.objects.filter(extracted_at__isnull=False).count() == 2
    assert LLMCall.objects.filter(task="extract_job", user__isnull=True).count() == 2


def test_partial_failures_are_recorded_but_the_run_succeeds(
    ew: Source, router: respx.MockRouter
) -> None:
    mock_listing(router, listing_of(["sourcing/823", "no-such-job"]))
    mock_details(router, only=["sourcing/823"])
    router.get("https://expertwoka.com/opportunities/no-such-job").mock(
        return_value=httpx.Response(404)
    )
    run = go(ew)
    assert run.success and run.new_jobs == 1
    assert run.errors == ["http_404:/opportunities/no-such-job"]


# --- site changes: health ----------------------------------------------------------------------------------


def test_a_site_with_no_job_links_is_degraded_not_a_crash(
    ew: Source, router: respx.MockRouter
) -> None:
    mock_listing(router, "<html><body>We redesigned everything.</body></html>")
    run = go(ew)
    ew.refresh_from_db()
    assert run.success is False and run.errors == ["no_ids_found"] and run.ids_seen == 0
    assert (ew.health, ew.consecutive_failures) == ("degraded", 1)


def test_unparseable_detail_pages_fail_the_run(ew: Source, router: respx.MockRouter) -> None:
    mock_listing(router, listing_of(["a-job", "b-job"]))
    for slug in ("a-job", "b-job"):
        router.get(f"https://expertwoka.com/opportunities/{slug}").mock(
            return_value=httpx.Response(200, text="<html><body>no job here</body></html>")
        )
    run = go(ew)
    assert run.success is False and run.new_jobs == 0
    assert all(e.startswith("unparseable_detail") for e in run.errors)
    ew.refresh_from_db()
    assert ew.health == "degraded"


def test_health_goes_ok_degraded_failing_and_recovers(
    ew: Source, router: respx.MockRouter, handler: ListHandler
) -> None:
    mock_listing(router, "<html></html>")
    states = []
    for _ in range(3):
        go(ew)
        ew.refresh_from_db()
        states.append((ew.health, ew.consecutive_failures))
    assert states == [("degraded", 1), ("degraded", 2), ("failing", 3)]
    changes = handler.events("source_health_changed")
    assert [(c["old"], c["new"], c["level"]) for c in changes] == [
        ("ok", "degraded", "WARNING"),
        ("degraded", "failing", "ERROR"),
    ]

    mock_listing(router, listing_of(["sourcing/823"]))
    mock_details(router, only=["sourcing/823"])
    go(ew)
    ew.refresh_from_db()
    assert (ew.health, ew.consecutive_failures) == ("ok", 0)


@pytest.mark.parametrize(
    ("failures", "health"),
    [(0, "ok"), (1, "degraded"), (2, "degraded"), (3, "failing"), (9, "failing")],
)
def test_health_for(failures: int, health: str) -> None:
    assert services.health_for(failures) == health


def test_rate_limiting_marks_the_run_failed_and_does_not_retry(
    ew: Source, router: respx.MockRouter
) -> None:
    route = router.get(LISTING_URL, params={"sort": "newest", "page": "1"}).mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"})
    )
    run = go(ew)
    ew.refresh_from_db()
    assert run.success is False and run.errors == ["rate_limited:retry_after=30s"]
    assert route.call_count == 1 and ew.health == "degraded"


def test_listing_http_errors_fail_the_run(ew: Source, router: respx.MockRouter) -> None:
    mock_listing(router, "boom", status=503)
    run = go(ew)
    assert run.success is False and run.errors[0].startswith("http_503")


def test_allow_empty_makes_an_empty_listing_a_success(router: respx.MockRouter) -> None:
    source = SourceFactory(
        kind="json_api",
        config={
            "url": "https://api.test/j",
            "id_field": "id",
            "map": {"title": "t"},
            "allow_empty": True,
        },
    )
    router.get("https://api.test/j").mock(return_value=httpx.Response(200, json=[]))
    run = go(source)
    assert run.success and run.ids_seen == 0 and run.errors == []


def test_an_unexpected_crash_is_contained_and_recorded(
    ew: Source, monkeypatch: pytest.MonkeyPatch, handler: ListHandler
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug in a fetcher")

    monkeypatch.setattr(services, "collect", boom)
    run = go(ew)
    ew.refresh_from_db()
    assert run.success is False and run.errors == ["crashed:RuntimeError"] and run.finished_at
    assert ew.consecutive_failures == 1 and handler.events("source_run_crashed")


def test_a_misconfigured_source_fails_cleanly() -> None:
    source = SourceFactory(kind="rss", config={})  # no url
    run = go(source)
    assert run.success is False and "config.url" in run.errors[0]


def test_unknown_plugin_name_fails_cleanly() -> None:
    source = SourceFactory(name="missing-plugin", kind="custom")
    run = go(source)
    assert run.success is False and run.errors[0] == "crashed:UnknownSourceKind"


# --- json_api sources: everything is returned every run -----------------------------------------------------------


def test_json_api_runs_update_existing_jobs(router: respx.MockRouter) -> None:
    source = SourceFactory(
        kind="json_api",
        config={
            "url": "https://api.test/j",
            "id_field": "id",
            "company": "ApiCo",
            "map": {"title": "t", "pay_text": "p"},
        },
    )
    route = router.get("https://api.test/j").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "t": "Role", "p": "$10/hr"}])
    )
    first = go(source)
    route.mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "t": "Role", "p": "$12/hr"}, {"id": 2, "t": "Other"}]
        )
    )
    second = go(source)
    assert (first.new_jobs, second.new_jobs, second.updated_jobs, second.ids_seen) == (1, 1, 1, 2)
    assert Job.objects.get(external_id="1").pay_max == 12


# --- overlap and scheduling ---------------------------------------------------------------------------------------


def test_a_running_source_is_not_run_twice(ew: Source, router: respx.MockRouter) -> None:
    SourceRun.objects.create(
        source=ew, started_at=timezone.now() - datetime.timedelta(minutes=2)
    )  # unfinished
    route = mock_listing(router, listing_of(FIVE))
    with http.build_client() as client:
        assert services.run_source(ew, client=client) is None
    assert not route.called and SourceRun.objects.count() == 1


def test_a_crashed_run_that_never_finished_does_not_block_forever(
    ew: Source, router: respx.MockRouter
) -> None:
    SourceRun.objects.create(source=ew, started_at=timezone.now() - datetime.timedelta(hours=2))
    mock_listing(router, listing_of(["sourcing/823"]))
    mock_details(router, only=["sourcing/823"])
    assert go(ew).success


def test_due_sources_respects_interval_enabled_and_never_run() -> None:
    now = timezone.now()
    never = SourceFactory(interval_minutes=20)
    due = SourceFactory(interval_minutes=20, last_run_at=now - datetime.timedelta(minutes=21))
    SourceFactory(interval_minutes=20, last_run_at=now - datetime.timedelta(minutes=19))  # not yet
    SourceFactory(enabled=False)
    SourceFactory(
        interval_minutes=60, last_run_at=now - datetime.timedelta(minutes=30)
    )  # slower source
    assert set(services.due_sources(now)) == {never, due}


def test_tick_runs_due_sources_through_the_queue(ew: Source, router: respx.MockRouter) -> None:
    mock_listing(router, listing_of(FIVE))
    mock_details(router, only=FIVE)
    tick()  # Huey runs tasks immediately in tests
    run = SourceRun.objects.get()
    assert run.trigger == "schedule" and run.success and run.new_jobs == 5
    tick()  # just ran: not due again for 20 minutes
    assert SourceRun.objects.count() == 1


def test_run_source_task_handles_unknown_sources_and_locks(
    ew: Source, router: respx.MockRouter
) -> None:
    from huey.contrib.djhuey import lock_task

    enqueue(run_source_task, 999_999)  # no such source: nothing happens
    mock_listing(router, listing_of(["sourcing/823"]))
    mock_details(router, only=["sourcing/823"])
    with lock_task(f"run_source:{ew.pk}"):
        enqueue(run_source_task, ew.pk, "admin")  # another worker holds the lock: skipped
    assert SourceRun.objects.count() == 0
    enqueue(run_source_task, ew.pk, "admin")
    assert SourceRun.objects.get().trigger == "admin"


# --- re-checking stale jobs and expiry ------------------------------------------------------------------------------


def stale_job(source: Source, external_id: str, days: int = 3) -> Job:
    return JobFactory(
        source=source,
        external_id=external_id,
        url=f"https://expertwoka.com/opportunities/{external_id}",
        last_seen_at=timezone.now() - datetime.timedelta(days=days),
    )


def test_recheck_expires_jobs_whose_page_is_gone(ew: Source, router: respx.MockRouter) -> None:
    gone = stale_job(ew, "gone-job")
    gone410 = stale_job(ew, "gone-410")
    alive = stale_job(ew, "alive-job")
    router.get(gone.url).mock(return_value=httpx.Response(404))
    router.get(gone410.url).mock(return_value=httpx.Response(410))
    router.get(alive.url).mock(return_value=httpx.Response(200, text="still here"))
    with http.build_client() as client:
        expired = services.recheck_stale_jobs(ew, client=client, sleep=lambda s: None)
    for job in (gone, gone410, alive):
        job.refresh_from_db()
    assert expired == 2 and (gone.status, gone410.status, alive.status) == (
        "expired",
        "expired",
        "active",
    )


def test_recheck_only_looks_at_jobs_unseen_for_two_days(
    ew: Source, router: respx.MockRouter
) -> None:
    fresh = stale_job(ew, "fresh", days=1)
    route = router.get(fresh.url).mock(return_value=httpx.Response(404))
    with http.build_client() as client:
        assert services.recheck_stale_jobs(ew, client=client) == 0
    assert not route.called


def test_recheck_is_capped_and_polite(ew: Source, router: respx.MockRouter) -> None:
    for i in range(25):
        job = stale_job(ew, f"job-{i}", days=3 + i % 3)
        router.get(job.url).mock(return_value=httpx.Response(200, text="ok"))
    sleeps: list[float] = []
    ew.config = {**ew.config, "delay_seconds": 1.5}
    ew.save()
    with http.build_client() as client:
        services.recheck_stale_jobs(ew, client=client, sleep=sleeps.append)
    assert len(sleeps) == services.RECHECK_LIMIT - 1  # 20 requests, a pause between each


def test_recheck_ignores_sources_that_cannot_tell(router: respx.MockRouter) -> None:
    api = SourceFactory(kind="json_api")
    job = JobFactory(source=api, last_seen_at=timezone.now() - datetime.timedelta(days=5))
    assert services.recheck_stale_jobs(api) == 0
    job.refresh_from_db()
    assert job.status == "active"


def test_the_daily_expiry_task_rechecks_then_expires_by_age(
    ew: Source, router: respx.MockRouter, handler: ListHandler, settings
) -> None:  # type: ignore[no-untyped-def]
    settings.JOB_EXPIRY_DAYS = 7
    gone = stale_job(ew, "gone-job", days=3)
    old = stale_job(ew, "old-job", days=10)
    router.get(gone.url).mock(return_value=httpx.Response(404))
    router.get(old.url).mock(
        return_value=httpx.Response(200, text="ok")
    )  # still up, but unseen for 10 days
    expire_stale()
    gone.refresh_from_db()
    old.refresh_from_db()
    assert gone.status == "expired" and old.status == "expired"
    (line,) = handler.events("expire_stale_finished")
    assert line["expired_by_recheck"] == 1 and line["expired_by_age"] == 1


# --- seeding and commands -------------------------------------------------------------------------------------------


def test_seed_sources_is_idempotent_and_keeps_admin_edits() -> None:
    assert services.seed_sources() == [("expertwoka", True)]
    Source.objects.filter(name="expertwoka").update(interval_minutes=45, enabled=False)
    assert services.seed_sources() == [("expertwoka", False)]
    source = Source.objects.get()
    assert (source.interval_minutes, source.enabled) == (45, False)


def test_seeded_expertwoka_matches_the_spec() -> None:
    services.seed_sources()
    source = Source.objects.get(name="expertwoka")
    assert (source.kind, source.interval_minutes, source.enabled) == ("custom", 20, True)
    assert (
        source.config["max_new_per_run"] == 30
        and "expertwoka.com/opportunities" in source.config["list_url"]
    )


def test_seed_sources_command() -> None:
    out = StringIO()
    call_command("seed_sources", stdout=out)
    call_command("seed_sources", stdout=out)
    assert (
        "expertwoka: created" in out.getvalue() and "expertwoka: already exists" in out.getvalue()
    )


def test_test_source_dry_run_prints_jobs_and_saves_nothing(
    ew: Source, router: respx.MockRouter
) -> None:
    mock_listing(router)  # the real saved listing page with its 12 IDs
    mock_details(router)
    out = StringIO()
    call_command("test_source", "expertwoka", "--dry-run", "--limit", "5", stdout=out)
    text = out.getvalue()
    assert "Personal Account Generalist Raters (Mobile)  [ExpertWoka Sourcing]" in text
    assert "locations: United States  (KE: not eligible)" in text
    assert "pay:       $25/hr  => 25-25 USD/hour" in text
    assert "Analytical Chemist" not in text or "(KE: eligible)" in text
    assert "12 id(s) seen" in text
    assert Job.objects.count() == 0 and SourceRun.objects.count() == 0


def test_test_source_dry_run_shows_global_jobs_as_eligible_for_kenya(
    ew: Source, router: respx.MockRouter
) -> None:
    mock_listing(router, listing_of(["analytical-chemist-mercor"]))
    mock_details(router, only=["analytical-chemist-mercor"])
    out = StringIO()
    call_command("test_source", "expertwoka", "--dry-run", stdout=out)
    assert "locations: Global  (KE: eligible)" in out.getvalue()
    assert "$65-$75 per task  => 65-75 USD/task" in out.getvalue()


def test_test_source_json_output(ew: Source, router: respx.MockRouter) -> None:
    mock_listing(router, listing_of(["sourcing/823"]))
    mock_details(router, only=["sourcing/823"])
    out = StringIO()
    call_command("test_source", "expertwoka", "--dry-run", "--json", stdout=out)
    payload = json.loads(out.getvalue().split("\n\n")[0])
    assert payload[0]["title"] == "Personal Account Generalist Raters (Mobile)"


def test_test_source_without_dry_run_really_runs(ew: Source, router: respx.MockRouter) -> None:
    mock_listing(router, listing_of(FIVE))
    mock_details(router, only=FIVE)
    out = StringIO()
    call_command("test_source", "expertwoka", "--limit", "2", stdout=out)
    assert "OK" in out.getvalue() and Job.objects.count() == 2
    assert SourceRun.objects.get().trigger == "manual"


def test_test_source_unknown_name_lists_what_exists() -> None:
    SourceFactory(name="known-one")
    with pytest.raises(CommandError, match="known-one"):
        call_command("test_source", "nope", "--dry-run")


def test_test_source_reports_problems_without_crashing(
    ew: Source, router: respx.MockRouter
) -> None:
    mock_listing(router, "<html></html>")
    out = StringIO()
    call_command("test_source", "expertwoka", "--dry-run", stdout=out)
    assert "0 job(s) parsed" in out.getvalue()


def test_a_source_that_goes_failing_notifies_linked_staff_once(
    ew: Source, router: respx.MockRouter, tg, django_capture_on_commit_callbacks
) -> None:
    from apps.notifications.models import Notification
    from tests.notif_helpers import make_notif_user

    staff = make_notif_user(chat_id="42", full_name="Owner")
    staff.is_staff = True
    staff.save()
    mock_listing(router, "<html></html>")
    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(4):  # failing from the third run on; the fourth must not repeat the notice
            go(ew)
    notices = Notification.objects.filter(user=staff, kind="source_health")
    assert notices.count() == 1 and notices.get().status == "sent"
    assert "ExpertWoka is failing" in tg.messages[0]["text"]
