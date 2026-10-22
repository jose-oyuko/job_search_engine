"""ExpertWoka plugin tests against the saved real pages in tests/fixtures/html/expertwoka/."""

import datetime

import httpx
import pytest
import respx

from apps.sources import http
from apps.sources.custom.expertwoka import ExpertWokaSource
from apps.sources.exceptions import SourceError
from tests.factories import SourceFactory
from tests.source_helpers import (
    DETAIL_FIXTURES,
    LISTING_IDS,
    LISTING_URL,
    mock_details,
    mock_listing,
    page,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def fetcher():  # type: ignore[no-untyped-def]
    source = SourceFactory.build(name="expertwoka", display_name="ExpertWoka", kind="custom")
    with http.build_client() as client:
        yield ExpertWokaSource(source, client, sleep=lambda s: None)


def parse(fetcher, external_id: str):  # type: ignore[no-untyped-def]
    html = page(DETAIL_FIXTURES[external_id])
    return fetcher.parse_detail(
        html, external_id, f"https://expertwoka.com/opportunities/{external_id}"
    )


# --- the listing -----------------------------------------------------------------------------------------


def test_list_ids_finds_the_twelve_jobs_newest_first(fetcher, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router)
    assert fetcher.list_ids() == LISTING_IDS


def test_list_ids_excludes_categories_pagination_and_other_pages(
    fetcher, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router)
    ids = fetcher.list_ids()
    assert all("?" not in i and not i.startswith("companies") for i in ids)
    assert len(ids) == len(set(ids))
    assert not any(i in ("", "sourcing", "sourcing/") for i in ids)


def test_list_ids_ignores_links_to_other_hosts_and_non_jobs(
    fetcher, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    html = """<a href="https://expertwoka.com/opportunities/real-job-acme">ok</a>
        <a href="https://evil.example/opportunities/steal-me">off-host</a>
        <a href="/opportunities/relative-job-acme">relative</a>
        <a href="https://expertwoka.com/opportunities?category=AI">category</a>
        <a href="https://expertwoka.com/opportunities/sourcing/12/apply">apply link</a>
        <a href="https://expertwoka.com/opportunities/Bad_Slug">uppercase</a>
        <a href="https://expertwoka.com/opportunities/real-job-acme#top">dup</a>"""
    mock_listing(router, html)
    assert fetcher.list_ids() == ["real-job-acme", "relative-job-acme"]


def test_a_listing_with_no_job_links_finds_nothing(fetcher, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router, "<html><body><p>Redesigned! No cards here.</p></body></html>")
    assert fetcher.list_ids() == []


# --- two-phase fetching ----------------------------------------------------------------------------------


def test_only_unseen_ids_trigger_detail_requests(fetcher, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router)
    routes = mock_details(router)
    seen = set(LISTING_IDS) - {"analytical-chemist-mercor", "finance-expert-afterquery"}

    jobs = list(fetcher.fetch_all_new(seen))

    assert sorted(j.external_id for j in jobs) == [
        "analytical-chemist-mercor",
        "finance-expert-afterquery",
    ]
    assert fetcher.ids_seen == LISTING_IDS
    called = [i for i, route in routes.items() if route.called]
    assert sorted(called) == ["analytical-chemist-mercor", "finance-expert-afterquery"]


def test_nothing_new_means_no_detail_requests(fetcher, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router)
    routes = mock_details(router)
    assert list(fetcher.fetch_all_new(set(LISTING_IDS))) == []
    assert not any(route.called for route in routes.values())


def test_max_new_per_run_and_limit_cap_the_detail_requests(
    fetcher, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router)
    routes = mock_details(router)
    fetcher.limit = 2
    list(fetcher.fetch_all_new(set()))
    assert sum(route.call_count for route in routes.values()) == 2

    fetcher.limit = None
    fetcher.config["max_new_per_run"] = 3
    before = sum(route.call_count for route in routes.values())
    list(fetcher.fetch_all_new(set()))
    assert sum(route.call_count for route in routes.values()) - before == 3


def test_a_failing_detail_page_is_recorded_and_the_rest_continue(
    fetcher, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    mock_listing(router)
    mock_details(router)
    router.get("https://expertwoka.com/opportunities/sourcing/822").mock(
        return_value=httpx.Response(500)
    )
    fetcher.limit = 5
    # Newest first: 823 ok, 822 fails (500 after retries), 821/820 are not mocked -> errors too.
    jobs = []
    for raw in fetcher.fetch_all_new(set()):
        jobs.append(raw)
    assert jobs and jobs[0].external_id == "sourcing/823"
    assert any("http_500" in e or "sourcing/822" in e for e in fetcher.errors)


def test_requests_are_spaced_out_politely(router: respx.MockRouter) -> None:
    mock_listing(router)
    mock_details(router)
    sleeps: list[float] = []
    source = SourceFactory.build(name="expertwoka", kind="custom", config={"max_new_per_run": 3})
    with http.build_client() as client:
        fetcher = ExpertWokaSource(source, client, sleep=sleeps.append)
        fetcher.config["delay_seconds"] = 1.5
        list(fetcher.fetch_all_new(set()))
    assert sleeps.count(1.5) == 2  # between 3 detail requests, not before the first


def test_requests_identify_the_scraper(fetcher, router: respx.MockRouter, settings) -> None:  # type: ignore[no-untyped-def]
    route = mock_listing(router)
    fetcher.list_ids()
    assert route.calls.last.request.headers["user-agent"] == settings.SCRAPER_USER_AGENT
    assert route.calls.last.request.url.params["sort"] == "newest"
    assert route.calls.last.request.url.params["page"] == "1"


# --- the five saved detail pages ------------------------------------------------------------------------


def test_global_page_without_jsonld(fetcher) -> None:  # type: ignore[no-untyped-def]
    job = parse(fetcher, "analytical-chemist-mercor")
    assert (job.title, job.company) == ("Analytical Chemist", "Mercor")
    assert job.locations == ["Global"] and job.work_type == "remote"
    assert job.pay_text == "$65-$75 per task"
    assert job.apply_url == "https://t.mercor.com/iPnXK"
    assert job.posted_at == datetime.datetime(2026, 10, 22, tzinfo=datetime.UTC)
    assert "red-team frontier AI models" in job.description_text
    assert "<h3>Overview</h3>" in job.description_html
    assert "Analytical Chemistry" in job.raw["skills"] and "GC-MS" in job.raw["skills"]
    assert job.raw["jsonld"] is None  # this page really has none


def test_page_without_pay(fetcher) -> None:  # type: ignore[no-untyped-def]
    job = parse(fetcher, "ai-benchmark-red-teaming-specialist-turing")
    assert job.company == "Turing" and job.pay_text == ""  # "Not specified" becomes empty
    assert job.title == "AI Benchmark & Red-Teaming Specialist"  # entities decoded
    assert job.apply_url == "https://work.turing.com/r/oZnNjQmJ6z"


def test_page_with_jsonld_and_country_restriction(fetcher) -> None:  # type: ignore[no-untyped-def]
    job = parse(fetcher, "finance-expert-afterquery")
    assert (job.title, job.company) == ("Finance Expert", "AfterQuery")
    assert job.locations == ["United States"] and job.pay_text == "$100 - $200/hr"
    assert job.apply_url.startswith("https://experts.afterquery.com/apply/")
    assert job.raw["jsonld"]["identifier"]["value"] == "814"


def test_country_specific_page(fetcher) -> None:  # type: ignore[no-untyped-def]
    job = parse(fetcher, "sms-data-collector-english-nigeria-productive-playhouse")
    assert job.locations == ["Nigeria"] and job.company == "Productive Playhouse"
    assert job.pay_text == "$1.50 USD per qualified message"
    assert job.apply_url.startswith("https://productiveplayhouse.applytojob.com/")


def test_expertwoka_sourcing_role(fetcher) -> None:  # type: ignore[no-untyped-def]
    job = parse(fetcher, "sourcing/823")
    assert job.title == "Personal Account Generalist Raters (Mobile)"
    assert job.company == "ExpertWoka Sourcing"  # the real client is confidential
    assert job.pay_text == "$25/hr" and job.locations == ["United States"]
    assert job.apply_url == "https://expertwoka.com/opportunities/sourcing/823/apply"
    assert job.url == "https://expertwoka.com/opportunities/sourcing/823"


def test_every_saved_page_yields_the_required_fields(fetcher) -> None:  # type: ignore[no-untyped-def]
    for external_id in DETAIL_FIXTURES:
        job = parse(fetcher, external_id)
        assert job.title and job.company and job.url and job.description_text
        assert job.locations and job.work_type == "remote" and job.posted_at
        assert job.source == fetcher.source.name and job.external_id == external_id


# --- when the site changes ----------------------------------------------------------------------------


def test_jsonld_fills_in_if_the_html_layout_changes(fetcher) -> None:  # type: ignore[no-untyped-def]
    html = page(DETAIL_FIXTURES["finance-expert-afterquery"]).replace(
        "ew-job-title", "renamed-class"
    )
    job = fetcher.parse_detail(
        html, "finance-expert-afterquery", "https://expertwoka.com/opportunities/x"
    )
    assert job.title == "Finance Expert" and job.company == "AfterQuery"
    assert job.locations == ["United States"] and job.work_type == "remote"


def test_a_changed_page_with_no_jsonld_is_an_error_not_a_crash(fetcher) -> None:  # type: ignore[no-untyped-def]
    html = page(DETAIL_FIXTURES["analytical-chemist-mercor"]).replace(
        "ew-job-title", "renamed-class"
    )
    with pytest.raises(SourceError, match="unparseable_detail"):
        fetcher.parse_detail(
            html, "analytical-chemist-mercor", "https://expertwoka.com/opportunities/x"
        )


def test_an_empty_page_is_an_error(fetcher) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SourceError):
        fetcher.parse_detail("<html></html>", "x", "https://expertwoka.com/opportunities/x")


def test_defaults_can_be_overridden_by_source_config() -> None:
    source = SourceFactory.build(
        name="expertwoka",
        kind="custom",
        config={
            "max_new_per_run": 5,
            "list_url": "https://expertwoka.com/opportunities?sort=newest&page=2",
        },
    )
    with http.build_client() as client:
        f = ExpertWokaSource(source, client)
    assert f.config["max_new_per_run"] == 5 and f.config["list_url"].endswith("page=2")
    assert f.config["link_pattern"]  # untouched defaults remain


def test_listing_url_constant_matches_the_documented_site() -> None:
    assert LISTING_URL == "https://expertwoka.com/opportunities"
