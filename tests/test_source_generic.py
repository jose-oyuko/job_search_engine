"""Generic fetchers (jsonld, json_api, rss, ats, llm_extract), the HTTP helper and the registry,
tested with small synthetic fixtures. No test touches a real site."""

import json
from typing import Any

import httpx
import pytest
import respx

from apps.llm.providers.fake import FakeProvider
from apps.sources import http, registry
from apps.sources.exceptions import (
    HTTPStatusFailure,
    RateLimited,
    SourceConfigError,
    SourceError,
    UnknownSourceKind,
)
from apps.sources.generic.ats import AtsSource
from apps.sources.generic.json_api import JsonApiSource, dig
from apps.sources.generic.jsonld import JsonLdSource, find_job_posting, posting_to_rawjob
from apps.sources.generic.llm_extract import LlmExtractSource, page_text
from apps.sources.generic.rss import RssSource
from tests.factories import SourceFactory

pytestmark = pytest.mark.django_db


def make(cls: type, router: respx.MockRouter | None = None, **config: Any):  # type: ignore[no-untyped-def]
    source = SourceFactory.build(name="synthetic", display_name="Synthetic Co", config=config)
    client = http.build_client()
    return cls(source, client, sleep=lambda s: None)


# --- HTTP helper ------------------------------------------------------------------------------------------


def test_fetch_retries_5xx_twice_with_backoff_then_succeeds(router: respx.MockRouter) -> None:
    route = router.get("https://x.test/a").mock(
        side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(200, text="ok")]
    )
    sleeps: list[float] = []
    with http.build_client() as client:
        assert http.fetch(client, "https://x.test/a", sleep=sleeps.append).text == "ok"
    assert route.call_count == 3 and sleeps == [1.0, 2.0]


def test_fetch_gives_up_after_two_retries(router: respx.MockRouter) -> None:
    route = router.get("https://x.test/a").mock(return_value=httpx.Response(502))
    with http.build_client() as client, pytest.raises(HTTPStatusFailure) as info:
        http.fetch(client, "https://x.test/a", sleep=lambda s: None)
    assert info.value.status == 502 and route.call_count == 3


def test_fetch_retries_timeouts(router: respx.MockRouter) -> None:
    route = router.get("https://x.test/a").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, text="ok")]
    )
    with http.build_client() as client:
        assert http.fetch(client, "https://x.test/a", sleep=lambda s: None).text == "ok"
    assert route.call_count == 2


def test_fetch_reports_network_errors_without_the_query_string(router: respx.MockRouter) -> None:
    router.get("https://x.test/a").mock(side_effect=httpx.ConnectError("down"))
    with http.build_client() as client, pytest.raises(SourceError) as info:
        http.fetch(client, "https://x.test/a?token=SECRET", params=None, sleep=lambda s: None)
    assert "SECRET" not in str(info.value) and "/a" in str(info.value)


def test_429_is_not_retried_and_carries_retry_after(router: respx.MockRouter) -> None:
    route = router.get("https://x.test/a").mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"})
    )
    with http.build_client() as client, pytest.raises(RateLimited) as info:
        http.fetch(client, "https://x.test/a", sleep=lambda s: None)
    assert info.value.retry_after == 30.0 and route.call_count == 1


def test_429_without_retry_after(router: respx.MockRouter) -> None:
    router.get("https://x.test/a").mock(return_value=httpx.Response(429))
    with http.build_client() as client, pytest.raises(RateLimited) as info:
        http.fetch(client, "https://x.test/a", sleep=lambda s: None)
    assert info.value.retry_after is None


@pytest.mark.parametrize("status", [403, 404, 410])
def test_other_4xx_are_not_retried(router: respx.MockRouter, status: int) -> None:
    route = router.get("https://x.test/a").mock(return_value=httpx.Response(status))
    with http.build_client() as client, pytest.raises(HTTPStatusFailure) as info:
        http.fetch(client, "https://x.test/a", sleep=lambda s: None)
    assert info.value.status == status and route.call_count == 1


def test_oversized_responses_are_refused(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http, "MAX_BODY_BYTES", 10)
    router.get("https://x.test/a").mock(return_value=httpx.Response(200, text="x" * 100))
    with http.build_client() as client, pytest.raises(SourceError, match="too_large"):
        http.fetch(client, "https://x.test/a", sleep=lambda s: None)


def test_client_uses_the_configured_user_agent(settings) -> None:  # type: ignore[no-untyped-def]
    settings.SCRAPER_USER_AGENT = "TestBot/9 (+https://example.test)"
    with http.build_client() as client:
        assert client.headers["user-agent"] == "TestBot/9 (+https://example.test)"
        assert client.timeout.read == 20


def test_fetch_logs_the_path_only(router: respx.MockRouter, handler) -> None:  # type: ignore[no-untyped-def]
    router.get("https://x.test/secret/path").mock(return_value=httpx.Response(404))
    with http.build_client() as client, pytest.raises(HTTPStatusFailure):
        http.fetch(
            client, "https://x.test/secret/path", params={"api_key": "SECRET"}, sleep=lambda s: None
        )
    (line,) = handler.events("source_fetch_error")
    assert line["path"] == "/secret/path" and "SECRET" not in "".join(handler.lines)


# --- registry ------------------------------------------------------------------------------------------------


def test_registry_has_every_builtin_kind() -> None:
    keys = registry.registered_keys()
    for key in ("jsonld", "ats", "json_api", "rss", "llm_extract", "custom:expertwoka"):
        assert key in keys


def test_registry_finds_the_fetcher_for_a_source() -> None:
    assert registry.fetcher_class(SourceFactory.build(kind="rss")) is RssSource
    assert (
        registry.fetcher_class(SourceFactory.build(name="expertwoka", kind="custom")).__name__
        == "ExpertWokaSource"
    )


def test_unknown_kinds_and_unregistered_plugins_raise() -> None:
    with pytest.raises(UnknownSourceKind):
        registry.fetcher_class(SourceFactory.build(kind="telepathy"))
    with pytest.raises(UnknownSourceKind):
        registry.fetcher_class(SourceFactory.build(name="no-such-plugin", kind="custom"))


# --- jsonld -----------------------------------------------------------------------------------------------------


POSTING = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Data Annotator",
    "description": "<p>Label images.</p><ul><li>Fast</li></ul>",
    "datePosted": "2026-10-20T08:00:00Z",
    "hiringOrganization": {"@type": "Organization", "name": "LabelCo"},
    "jobLocationType": "TELECOMMUTE",
    "applicantLocationRequirements": [{"@type": "Country", "name": "Kenya"}, {"name": "Uganda"}],
    "baseSalary": {
        "@type": "MonetaryAmount",
        "currency": "USD",
        "value": {"@type": "QuantitativeValue", "minValue": 10, "maxValue": 15, "unitText": "HOUR"},
    },
    "employmentType": ["CONTRACTOR", "PART_TIME"],
    "url": "https://labelco.test/apply/1",
}


def ld(obj: Any) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def test_find_job_posting_handles_graph_lists_and_bad_blocks() -> None:
    assert find_job_posting(ld(POSTING)) == POSTING
    assert find_job_posting(ld({"@graph": [{"@type": "WebSite"}, POSTING]})) == POSTING
    assert find_job_posting(ld([{"@type": "Organization"}, POSTING])) == POSTING
    assert (
        find_job_posting(ld({**POSTING, "@type": ["Thing", "JobPosting"]}))["title"]
        == "Data Annotator"
    )
    broken = '<script type="application/ld+json">{not json</script>' + ld(POSTING)
    assert find_job_posting(broken) == POSTING
    assert find_job_posting("<html></html>") is None
    assert find_job_posting(ld({"@type": "Product"})) is None


def test_posting_to_rawjob_maps_the_fields() -> None:
    job = posting_to_rawjob(POSTING, source="s", external_id="1", url="https://x.test/1")
    assert (job.title, job.company, job.work_type) == ("Data Annotator", "LabelCo", "remote")
    assert job.locations == ["Kenya", "Uganda"]
    assert job.pay_text == "USD 10-15 per hour"
    assert job.employment_type == "CONTRACTOR, PART_TIME"
    assert job.apply_url == "https://labelco.test/apply/1"
    assert job.description_text == "Label images.\n\n- Fast"
    assert job.posted_at.isoformat() == "2026-10-20T08:00:00+00:00"


def test_posting_locations_from_addresses() -> None:
    posting = {
        **POSTING,
        "applicantLocationRequirements": None,
        "jobLocation": {"address": {"addressLocality": "Nairobi", "addressCountry": "KE"}},
    }
    job = posting_to_rawjob(posting, source="s", external_id="1", url="u")
    assert job.locations == ["Nairobi", "KE"]


def test_posting_without_a_title_is_an_error() -> None:
    with pytest.raises(SourceError, match="without_title"):
        posting_to_rawjob({"@type": "JobPosting"}, source="s", external_id="1", url="u")


def test_jsonld_source_end_to_end(router: respx.MockRouter) -> None:
    router.get("https://jobs.test/list").mock(
        return_value=httpx.Response(
            200,
            text="""<a class="job" href="/jobs/101">a</a><a class="job" href="/jobs/102">b</a>
                    <a class="job" href="https://elsewhere.test/jobs/999">c</a>
                    <a class="nav" href="/about">d</a>""",
        )
    )
    detail = router.get(url__regex=r"https://jobs\.test/jobs/\d+").mock(
        return_value=httpx.Response(200, text=ld(POSTING))
    )
    fetcher = make(
        JsonLdSource,
        list_url="https://jobs.test/list",
        link_selector="a.job",
        link_pattern=r"^/jobs/\d+$",
    )
    jobs = list(fetcher.fetch_all_new({"101"}))
    assert fetcher.ids_seen == ["101", "102"]
    assert [j.external_id for j in jobs] == ["102"] and detail.call_count == 1
    assert jobs[0].url == "https://jobs.test/jobs/102" and jobs[0].source == "synthetic"


def test_jsonld_id_from_regex() -> None:
    fetcher = make(
        JsonLdSource, list_url="https://jobs.test/", id_from="regex", id_regex=r"/job-(\d+)-"
    )
    assert fetcher.external_id_for("https://jobs.test/job-42-data-annotator") == "42"
    assert fetcher.external_id_for("https://jobs.test/other") is None


def test_jsonld_page_without_a_posting_is_a_recorded_error(router: respx.MockRouter) -> None:
    router.get("https://jobs.test/list").mock(
        return_value=httpx.Response(200, text='<a href="/jobs/1">x</a>')
    )
    router.get("https://jobs.test/jobs/1").mock(
        return_value=httpx.Response(200, text="<html>no ld</html>")
    )
    fetcher = make(JsonLdSource, list_url="https://jobs.test/list", link_pattern=r"^/jobs/\d+$")
    assert list(fetcher.fetch_all_new(set())) == []
    assert fetcher.errors == ["no_jobposting:/jobs/1"]


def test_jsonld_requires_a_list_url() -> None:
    with pytest.raises(SourceConfigError):
        make(JsonLdSource).list_ids()


# --- json_api ---------------------------------------------------------------------------------------------------


API = {
    "data": {
        "jobs": [
            {
                "id": 7,
                "name": "Support Agent",
                "org": {"title": "HelpCo"},
                "link": "https://helpco.test/7",
                "body": "<p>Help people.</p>",
                "salary": "$12/hr",
                "where": [{"name": "Kenya"}, {"name": "Ghana"}],
                "mode": "Remote",
                "created": 1760000000000,
            },
            {"id": 8, "name": "", "org": {"title": "HelpCo"}},
            {"id": 9, "name": "Night Agent", "org": {"title": "HelpCo"}, "where": "Global"},
        ]
    }
}
API_CONFIG = {
    "url": "https://api.test/jobs",
    "params": {"page": 1},
    "items_path": "data.jobs",
    "id_field": "id",
    "company": "Fallback Inc",
    "map": {
        "title": "name", "company": "org.title", "url": "link", "description_html": "body",
        "pay_text": "salary", "locations": "where", "work_type": "mode", "posted_at": "created",
    },
}  # fmt: skip


def test_dig_follows_dicts_and_list_indexes() -> None:
    data = {"a": {"b": [{"c": 5}, {"c": 6}]}}
    assert dig(data, "a.b.1.c") == 6
    assert dig(data, "a.x.y") is None and dig(data, "a.b.9") is None and dig(data, "a.b.zz") is None


def test_json_api_maps_items_and_returns_everything(router: respx.MockRouter) -> None:
    route = router.get("https://api.test/jobs", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=API)
    )
    fetcher = make(JsonApiSource, **API_CONFIG)
    jobs = list(
        fetcher.fetch_all_new({"7"})
    )  # already-seen IDs are still returned: upsert refreshes
    assert route.call_count == 1 and fetcher.ids_seen == ["7", "8", "9"]
    first, second = jobs
    assert (first.external_id, first.title, first.company) == ("7", "Support Agent", "HelpCo")
    assert first.locations == ["Kenya", "Ghana"] and first.work_type == "remote"
    assert first.posted_at.year == 2025  # epoch milliseconds
    assert first.description_html == "<p>Help people.</p>" and first.pay_text == "$12/hr"
    assert second.locations == ["Global"]
    assert fetcher.errors == ["item_without_title:8"]


def test_json_api_company_falls_back_to_config(router: respx.MockRouter) -> None:
    router.get("https://api.test/jobs").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "t": "Job"}])
    )
    fetcher = make(
        JsonApiSource,
        url="https://api.test/jobs",
        id_field="id",
        map={"title": "t"},
        company="Fallback Inc",
    )
    (job,) = list(fetcher.fetch_all_new(set()))
    assert job.company == "Fallback Inc"


def test_json_api_post_method_and_limit(router: respx.MockRouter) -> None:
    route = router.post("https://api.test/jobs").mock(return_value=httpx.Response(200, json=API))
    fetcher = make(JsonApiSource, **{**API_CONFIG, "method": "POST"})
    fetcher.limit = 1
    assert len(list(fetcher.fetch_all_new(set()))) == 1 and route.called


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="<html>not json</html>"), "invalid_json"),
        (httpx.Response(200, json={"data": {"jobs": "nope"}}), "items_path_not_a_list"),
    ],
)
def test_json_api_bad_payloads(
    router: respx.MockRouter, response: httpx.Response, message: str
) -> None:
    router.get("https://api.test/jobs").mock(return_value=response)
    with pytest.raises(SourceError, match=message):
        list(make(JsonApiSource, **API_CONFIG).fetch_all_new(set()))


@pytest.mark.parametrize("missing", ["url", "id_field", "map"])
def test_json_api_requires_its_config(missing: str) -> None:
    config = {k: v for k, v in API_CONFIG.items() if k != missing}
    with pytest.raises(SourceConfigError, match=missing):
        list(make(JsonApiSource, **config).fetch_all_new(set()))


# --- rss --------------------------------------------------------------------------------------------------------


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Acme Careers</title>
<item><title>Backend Dev</title><link>https://acme.test/1</link><guid>g-1</guid>
<description>&lt;p&gt;Write &lt;b&gt;code&lt;/b&gt;&lt;script&gt;x()&lt;/script&gt;&lt;/p&gt;</description>
<pubDate>Tue, 20 Oct 2026 08:00:00 +0000</pubDate><company>Hired Inc</company></item>
<item><title>Designer</title><link>https://acme.test/2</link></item>
<item><link>https://acme.test/3</link></item></channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Jobs</title>
<entry><title>QA Engineer</title><link href="https://atom.test/q"/><id>urn:q</id>
<summary>Test things</summary><updated>2026-10-21T10:00:00Z</updated></entry></feed>"""


def test_rss_items_become_rawjobs(router: respx.MockRouter) -> None:
    router.get("https://feed.test/rss").mock(return_value=httpx.Response(200, content=RSS.encode()))
    fetcher = make(RssSource, url="https://feed.test/rss", company_from="feed_title")
    jobs = list(fetcher.fetch_all_new(set()))
    assert [j.title for j in jobs] == ["Backend Dev", "Designer"]
    assert [j.external_id for j in jobs] == ["g-1", "https://acme.test/2"]  # guid, else link
    assert jobs[0].company == "Acme Careers" and jobs[0].url == "https://acme.test/1"
    assert "<script" not in jobs[0].description_html and jobs[0].description_text == "Write code"
    assert jobs[0].posted_at.isoformat() == "2026-10-20T08:00:00+00:00"
    assert fetcher.ids_seen == ["g-1", "https://acme.test/2", "https://acme.test/3"]
    assert fetcher.errors and fetcher.errors[0].startswith("item_without_title")


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"company_from": "feed_title"}, "Acme Careers"),
        ({"company_from": "item_field"}, "Hired Inc"),
        ({"company_from": "fixed", "company": "Fixed Co"}, "Fixed Co"),
    ],
)
def test_rss_company_modes(router: respx.MockRouter, config: dict[str, str], expected: str) -> None:
    router.get("https://feed.test/rss").mock(return_value=httpx.Response(200, content=RSS.encode()))
    jobs = list(make(RssSource, url="https://feed.test/rss", **config).fetch_all_new(set()))
    assert jobs[0].company == expected


def test_atom_feeds_work_too(router: respx.MockRouter) -> None:
    router.get("https://feed.test/atom").mock(
        return_value=httpx.Response(200, content=ATOM.encode())
    )
    (job,) = make(RssSource, url="https://feed.test/atom", company_from="feed_title").fetch_all_new(
        set()
    )
    assert (job.title, job.url, job.external_id, job.company) == (
        "QA Engineer",
        "https://atom.test/q",
        "urn:q",
        "Atom Jobs",
    )
    assert job.posted_at.isoformat() == "2026-10-21T10:00:00+00:00"


def test_rss_refuses_hostile_xml(router: respx.MockRouter) -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><rss><channel><title>&b;</title></channel></rss>'
    router.get("https://feed.test/rss").mock(return_value=httpx.Response(200, content=bomb))
    with pytest.raises(SourceError, match="invalid_feed"):
        list(make(RssSource, url="https://feed.test/rss").fetch_all_new(set()))


def test_rss_rejects_non_xml_and_missing_url(router: respx.MockRouter) -> None:
    router.get("https://feed.test/rss").mock(
        return_value=httpx.Response(200, content=b"<html><body>oops")
    )
    with pytest.raises(SourceError, match="invalid_feed"):
        list(make(RssSource, url="https://feed.test/rss").fetch_all_new(set()))
    with pytest.raises(SourceConfigError):
        list(make(RssSource).fetch_all_new(set()))


# --- ats: Greenhouse and Lever (payloads shaped like the live APIs checked in Phase 4) ----------------------


GREENHOUSE = {
    "jobs": [
        {
            "id": 7789554,
            "title": "Support Engineer",
            "absolute_url": "https://careers.acme.test/positions/7789554",
            "location": {"name": "Nairobi, Kenya"},
            "content": "&lt;p&gt;Fix &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
            "first_published": "2026-04-13T04:49:05-04:00",
            "updated_at": "2026-09-29T20:00:54-04:00",
            "company_name": "Acme",
            "metadata": [
                {
                    "id": 1,
                    "name": "Workplace Type",
                    "value": "Hybrid",
                    "value_type": "single_select",
                }
            ],
        },
        {"id": 2, "title": "", "location": None},
    ],
    "meta": {"total": 2},
}
LEVER = [
    {
        "id": "ad208490-4052-4f91-a57e-433f2d1e484b",
        "text": "Customer Success Manager",
        "hostedUrl": "https://jobs.lever.co/acme/ad2",
        "applyUrl": "https://jobs.lever.co/acme/ad2/apply",
        "categories": {
            "commitment": "Full Time",
            "location": "Atlanta",
            "allLocations": ["Atlanta", "Remote - US"],
        },
        "description": "<div>Great role</div>",
        "descriptionPlain": "Great role",
        "lists": [{"text": "Skills:", "content": "<li>SaaS</li>"}],
        "additional": "<p>Perks</p>",
        "workplaceType": "remote",
        "createdAt": 1788199939703,
        "salaryRange": {
            "min": 50000,
            "max": 70000,
            "currency": "USD",
            "interval": "per-year-salary",
        },
    }
]


def test_greenhouse_board(router: respx.MockRouter) -> None:
    route = router.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=GREENHOUSE)
    )
    fetcher = make(AtsSource, ats="greenhouse", board="acme", company="Acme Inc")
    jobs = list(fetcher.fetch_all_new({"7789554"}))  # everything is returned each run
    assert route.calls.last.request.url.params["content"] == "true"
    (job,) = jobs
    assert (job.external_id, job.title, job.company) == ("7789554", "Support Engineer", "Acme")
    assert job.locations == ["Nairobi, Kenya"] and job.work_type == "hybrid"
    assert job.description_html == "<p>Fix &amp; ship</p><ul><li>Python</li></ul>"  # unescaped once
    assert job.description_text == "Fix & ship\n\n- Python"
    assert job.apply_url == job.url == "https://careers.acme.test/positions/7789554"
    assert job.posted_at.year == 2026 and "content" not in job.raw
    assert fetcher.ids_seen == ["7789554", "2"] and fetcher.errors == ["posting_without_title:2"]


def test_lever_board(router: respx.MockRouter) -> None:
    route = router.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=LEVER)
    )
    fetcher = make(AtsSource, ats="lever", board="acme", company="Acme Inc")
    (job,) = list(fetcher.fetch_all_new(set()))
    assert route.calls.last.request.url.params["mode"] == "json"
    assert (job.title, job.company, job.employment_type) == (
        "Customer Success Manager",
        "Acme Inc",
        "Full Time",
    )
    assert job.locations == ["Atlanta", "Remote - US"] and job.work_type == "remote"
    assert (
        job.apply_url == "https://jobs.lever.co/acme/ad2/apply"
        and job.url == "https://jobs.lever.co/acme/ad2"
    )
    assert "<h3>Skills:</h3>" in job.description_html and "<p>Perks</p>" in job.description_html
    assert job.description_text == "Great role"
    assert job.pay_text == "USD 50000-70000 per year salary"
    assert job.posted_at.year == 2026 and "description" not in job.raw


def test_ats_config_errors() -> None:
    with pytest.raises(SourceConfigError, match="greenhouse, lever"):
        make(AtsSource, ats="workday", board="x").list_ids()
    with pytest.raises(SourceConfigError, match="board"):
        make(AtsSource, ats="lever").list_ids()


@pytest.mark.parametrize(
    ("ats", "payload"), [("greenhouse", {"unexpected": 1}), ("lever", {"unexpected": 1})]
)
def test_ats_unexpected_payloads(router: respx.MockRouter, ats: str, payload: Any) -> None:
    router.get(url__startswith="https://").mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(SourceError, match="unexpected_payload"):
        list(make(AtsSource, ats=ats, board="acme").fetch_all_new(set()))


def test_ats_limit(router: respx.MockRouter) -> None:
    router.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=LEVER * 3)
    )
    fetcher = make(AtsSource, ats="lever", board="acme")
    fetcher.limit = 1
    assert len(list(fetcher.fetch_all_new(set()))) == 1


# --- llm_extract ---------------------------------------------------------------------------------------------------


PAGE = """<html><head><title>x</title><style>.a{}</style><script>track()</script></head><body>
<nav>Home | Jobs | Login</nav><header>Site header</header>
<main><h1>Support Engineer</h1><p>Help customers over chat and email for Example Co. Remote role.</p></main>
<footer>Copyright</footer></body></html>"""


def test_page_text_drops_chrome_and_scripts() -> None:
    text = page_text(PAGE)
    assert "Support Engineer" in text and "Help customers" in text
    for noise in ("track()", "Home | Jobs", "Site header", "Copyright", ".a{}"):
        assert noise not in text


def _llm_source(router: respx.MockRouter) -> LlmExtractSource:
    router.get("https://small.test/careers").mock(
        return_value=httpx.Response(200, text='<a href="/careers/1">one</a>')
    )
    router.get("https://small.test/careers/1").mock(return_value=httpx.Response(200, text=PAGE))
    return make(
        LlmExtractSource,
        list_url="https://small.test/careers",
        link_pattern=r"^/careers/\d+$",
        company="Small Co",
    )


def test_llm_extract_builds_a_rawjob_from_the_models_answer(
    router: respx.MockRouter, fake_llm: FakeProvider
) -> None:
    (job,) = list(_llm_source(router).fetch_all_new(set()))
    assert (job.title, job.company, job.work_type, job.employment_type) == (
        "Support Engineer",
        "Example Co",
        "remote",
        "contract",
    )
    assert job.pay_text == "$20 per hour" and job.locations == ["Kenya"]
    assert job.url == "https://small.test/careers/1" and job.raw["extracted_by"] == "llm"
    sent = fake_llm.calls[0]
    assert (
        "<page>" in sent["messages"][0]["content"]
        and "Help customers" in sent["messages"][0]["content"]
    )
    assert "track()" not in sent["messages"][0]["content"]


def test_llm_extract_caches_per_page_content(
    router: respx.MockRouter, fake_llm: FakeProvider
) -> None:
    source = _llm_source(router)
    list(source.fetch_all_new(set()))
    list(source.fetch_all_new(set()))
    assert len(fake_llm.calls) == 1


def test_llm_extract_rejects_pages_that_are_not_jobs(
    router: respx.MockRouter, fake_llm: FakeProvider
) -> None:
    fake_llm.script.append(json.dumps({"is_job_posting": False}))
    source = _llm_source(router)
    assert list(source.fetch_all_new(set())) == []
    assert source.errors == ["not_a_job_posting:/careers/1"]


def test_llm_extract_survives_llm_failures(
    router: respx.MockRouter, fake_llm: FakeProvider
) -> None:
    fake_llm.script.extend(["junk", "more junk"])
    source = _llm_source(router)
    assert list(source.fetch_all_new(set())) == []
    assert source.errors and source.errors[0].startswith("llm_error:LLMOutputError")


def test_llm_extract_ignores_near_empty_pages(
    router: respx.MockRouter, fake_llm: FakeProvider
) -> None:
    router.get("https://small.test/careers").mock(
        return_value=httpx.Response(200, text='<a href="/careers/1">one</a>')
    )
    router.get("https://small.test/careers/1").mock(
        return_value=httpx.Response(200, text="<html><body>Hi</body></html>")
    )
    source = make(
        LlmExtractSource, list_url="https://small.test/careers", link_pattern=r"^/careers/\d+$"
    )
    assert list(source.fetch_all_new(set())) == [] and source.errors == ["empty_page:/careers/1"]
    assert fake_llm.calls == []


def test_llm_extract_default_is_ten_new_pages_per_run() -> None:
    assert make(LlmExtractSource, list_url="https://small.test/").max_new == 10
