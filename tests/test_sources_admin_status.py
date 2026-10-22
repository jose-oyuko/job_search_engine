"""Admin for sources and jobs, and the staff status page."""

import datetime

import httpx
import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.jobs.models import Job
from apps.llm.models import LLMCall
from apps.sources.models import Source, SourceRun
from tests.factories import JobFactory, SourceFactory, UserFactory
from tests.source_helpers import mock_details, mock_listing

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def admin_client() -> Client:
    client = Client()
    client.force_login(User.objects.create_superuser("root@example.com", "pw-12345-test"))
    return client


# --- source admin -------------------------------------------------------------------------------------------


def test_source_list_shows_health_and_job_counts(admin_client: Client) -> None:
    source = SourceFactory(
        name="expertwoka", display_name="ExpertWoka", health="failing", consecutive_failures=3
    )
    JobFactory(source=source)
    JobFactory(source=source)
    html = admin_client.get(reverse("admin:sources_source_changelist")).content.decode()
    assert "ExpertWoka" in html and "Failing" in html and "#b91c1c" in html  # red badge
    assert '<td class="field-job_count">2</td>' in html


def test_source_change_page_shows_only_the_ten_latest_runs(admin_client: Client) -> None:
    source = SourceFactory()
    for i in range(14):
        SourceRun.objects.create(
            source=source,
            started_at=timezone.now() - datetime.timedelta(minutes=i),
            new_jobs=i,
            success=True,
        )
    html = admin_client.get(
        reverse("admin:sources_source_change", args=[source.pk])
    ).content.decode()
    assert "Recent runs (latest 10)" in html
    assert html.count('class="field-started_at"') == 10


def test_health_fields_are_read_only_in_the_source_form(admin_client: Client) -> None:
    source = SourceFactory(health="degraded")
    admin_client.post(
        reverse("admin:sources_source_change", args=[source.pk]),
        {
            "name": source.name,
            "display_name": "Renamed",
            "kind": "rss",
            "config": "{}",
            "interval_minutes": 30,
            "enabled": "on",
            "notes": "",
            "health": "ok",
            "runs-TOTAL_FORMS": 0,
            "runs-INITIAL_FORMS": 0,
            "runs-MIN_NUM_FORMS": 0,
            "runs-MAX_NUM_FORMS": 0,
        },
    )
    source.refresh_from_db()
    assert source.display_name == "Renamed" and source.health == "degraded"


def test_run_now_action_queues_runs_and_the_results_appear(
    admin_client: Client, router: respx.MockRouter
) -> None:
    from apps.sources.services import seed_sources

    seed_sources()
    source = Source.objects.get(name="expertwoka")
    mock_listing(router, '<a href="https://expertwoka.com/opportunities/sourcing/823">x</a>')
    mock_details(router, only=["sourcing/823"])
    response = admin_client.post(
        reverse("admin:sources_source_changelist"),
        {"action": "run_now", "_selected_action": [source.pk]},
        follow=True,
    )
    assert "Queued 1 run(s)" in response.content.decode()
    run = SourceRun.objects.get()  # Huey runs immediately in tests
    assert run.trigger == "admin" and run.success and Job.objects.filter(source=source).count() == 1


def test_source_runs_are_read_only(admin_client: Client) -> None:
    run = SourceRun.objects.create(source=SourceFactory(), started_at=timezone.now())
    assert admin_client.get(reverse("admin:sources_sourcerun_add")).status_code == 403
    response = admin_client.post(
        reverse("admin:sources_sourcerun_change", args=[run.pk]), {"success": "on"}
    )
    run.refresh_from_db()
    assert run.success is False and response.status_code in (200, 403)


# --- job admin ----------------------------------------------------------------------------------------------


def test_job_list_search_and_filters(admin_client: Client) -> None:
    source = SourceFactory(display_name="Src")
    JobFactory(source=source, title="Prompt Engineer", company="AlphaCo")
    JobFactory(source=source, title="Data Rater", company="BetaCo", status="expired")
    url = reverse("admin:jobs_job_changelist")
    assert "Prompt Engineer" in admin_client.get(url, {"q": "alpha"}).content.decode()
    assert "Data Rater" not in admin_client.get(url, {"q": "alpha"}).content.decode()
    expired = admin_client.get(url, {"status__exact": "expired"}).content.decode()
    assert "Data Rater" in expired and "Prompt Engineer" not in expired


def test_job_change_page_shows_raw_and_extracted_json(admin_client: Client) -> None:
    job = JobFactory(
        raw={"stats": {"location": "Global"}}, extracted={"summary": "Rate AI answers"}
    )
    html = admin_client.get(reverse("admin:jobs_job_change", args=[job.pk])).content.decode()
    assert "&quot;stats&quot;" in html and "Rate AI answers" in html


def test_job_without_extraction_says_so(admin_client: Client) -> None:
    job = JobFactory()
    assert (
        "Not extracted yet."
        in admin_client.get(reverse("admin:jobs_job_change", args=[job.pk])).content.decode()
    )


def test_admin_pages_are_staff_only() -> None:
    client = Client()
    client.force_login(UserFactory())
    for name in ("admin:sources_source_changelist", "admin:jobs_job_changelist"):
        assert client.get(reverse(name)).status_code == 302


# --- the staff status page ------------------------------------------------------------------------------------


def test_status_page_requires_staff() -> None:
    assert "/accounts/login/" in Client().get("/app/staff/status/")["Location"]
    client = Client()
    client.force_login(UserFactory())
    assert client.get("/app/staff/status/").status_code == 403


def test_status_page_shows_sources_runs_and_spend(admin_client: Client) -> None:
    source = SourceFactory(
        display_name="ExpertWoka",
        health="degraded",
        consecutive_failures=1,
        last_run_at=timezone.now(),
    )
    JobFactory(source=source)
    JobFactory(source=source, first_seen_at=timezone.now() - datetime.timedelta(days=3))
    JobFactory(source=source, status="expired")
    SourceRun.objects.create(
        source=source,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        success=False,
        ids_seen=0,
        errors=["no_ids_found"],
    )
    LLMCall.objects.create(
        user=None, task="extract_job", provider="fake", model="m", cost_usd="0.25"
    )

    html = admin_client.get("/app/staff/status/").content.decode()

    assert "System status" in html and "ExpertWoka" in html and "Degraded" in html
    assert "Failed" in html and "$0.2500" in html
    # 3 jobs: 2 active (one older than 24h) and 1 expired -> active 2, new in 24h 2 (expired is new too)
    row = html[html.index("ExpertWoka") :]
    assert '<td class="py-2 pr-4">2</td>' in row
    assert "Jobs awaiting extraction" in html


def test_status_page_with_no_sources_explains_how_to_seed(admin_client: Client) -> None:
    html = admin_client.get("/app/staff/status/").content.decode()
    assert "seed_sources" in html and "No runs yet." in html


def test_staff_link_appears_in_the_nav_only_for_staff(admin_client: Client) -> None:
    assert "/app/staff/status/" in admin_client.get("/app/settings/account/").content.decode()
    client = Client()
    client.force_login(UserFactory())
    assert "/app/staff/status/" not in client.get("/app/settings/account/").content.decode()


def test_status_page_counts_pending_extraction(admin_client: Client) -> None:
    JobFactory(extracted_at=None)
    JobFactory(extracted_at=timezone.now())
    original = JobFactory(extracted_at=None)
    JobFactory(extracted_at=None, duplicate_of=original)  # duplicates never need extraction
    html = admin_client.get("/app/staff/status/").content.decode()
    assert 'Jobs awaiting extraction</dt><dd class="font-medium">2</dd>' in html


def test_no_real_http_in_these_tests(router: respx.MockRouter) -> None:
    router.get("https://example.test/").mock(return_value=httpx.Response(200))
    with pytest.raises(Exception):  # noqa: B017  (respx raises its own assertion type)
        httpx.get("https://not-mocked.test/")
