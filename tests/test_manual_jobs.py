"""Manual job input (spec §8): paste a URL or a description. Fetching is mocked with respx and DNS is
faked, so nothing touches the network."""

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
from django.test import Client

from apps.accounts.models import UserSettings
from apps.core import ssrf
from apps.jobs import manual, matching_hook
from apps.jobs.manual import BlockedSite, ManualJobError, NeedsDetails
from apps.jobs.models import Job
from apps.llm.exceptions import QuotaExceeded
from apps.llm.models import LLMCall, UsageQuota
from apps.llm.providers.fake import FakeProvider
from apps.sources.services import seed_sources
from tests.factories import JobFactory, UserFactory
from tests.logcapture import ListHandler
from tests.source_helpers import page as saved_page

pytestmark = pytest.mark.django_db

PUBLIC = "93.184.216.34"
DNS: dict[str, list[str]] = {}

POSTING = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Data Annotator",
    "description": "<p>Label images for AI training.</p>",
    "datePosted": "2026-10-20",
    "hiringOrganization": {"@type": "Organization", "name": "LabelCo"},
    "jobLocationType": "TELECOMMUTE",
    "applicantLocationRequirements": [{"@type": "Country", "name": "Kenya"}],
    "baseSalary": {
        "currency": "USD",
        "value": {"minValue": 10, "maxValue": 15, "unitText": "HOUR"},
    },
}
JSONLD_PAGE = f'<html><body><script type="application/ld+json">{json.dumps(POSTING)}</script><p>Apply now</p></body></html>'
PLAIN_PAGE = (
    "<html><body><nav>Home</nav><h1>Support Engineer</h1>"
    "<p>Help customers over chat and email for Example Co. This is a remote contract role open to "
    "people in Kenya, paying twenty dollars per hour.</p></body></html>"
)
TEXT = (
    "Customer Support Agent at Acme. We need someone to answer customer emails and chats, five "
    "hours a day, fully remote. Fluent English required. Apply by sending your CV to jobs@acme.test."
)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every name resolves to a public address unless DNS says otherwise; IP literals stay as they are."""
    DNS.clear()

    def resolver(host: str) -> list[str]:
        if host in DNS:
            return DNS[host]
        literal = ssrf._literal_address(host)  # IP literals, in every spelling, stay as they are
        return [literal] if literal else [PUBLIC]

    monkeypatch.setattr(ssrf, "system_resolver", resolver)


@pytest.fixture
def user():  # type: ignore[no-untyped-def]
    user = UserFactory()
    UserSettings.objects.create(user=user, onboarding_completed=True)
    return user


def serve(
    router: respx.MockRouter, path: str, body: str, content_type: str = "text/html"
) -> respx.Route:
    return router.get(f"https://{PUBLIC}{path}").mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": content_type})
    )


def extract_calls() -> int:
    return LLMCall.objects.filter(task="extract_page", cache_hit=False).count()


# --- URL canonicalisation and blocking ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com/jobs/1", "https://example.com/jobs/1"),
        ("  https://Example.COM/jobs/1#apply ", "https://example.com/jobs/1"),
        (
            "https://example.com/jobs/1?utm_source=x&gclid=1&id=7&ref=tw",
            "https://example.com/jobs/1?id=7",
        ),
        ("https://example.com:443/jobs/1", "https://example.com/jobs/1"),
        ("http://example.com:80/", "http://example.com"),
        ("https://example.com:80/x", "https://example.com:80/x"),
    ],
)
def test_canonical_url(raw: str, expected: str) -> None:
    assert manual.canonical_url(manual.normalise_input_url(raw)) == expected


@pytest.mark.parametrize("raw", ["", "   ", "x" * 2001])
def test_bad_input_is_rejected(raw: str) -> None:
    with pytest.raises(ManualJobError):
        manual.normalise_input_url(raw)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/123456",
        "https://uk.linkedin.com/jobs/view/1",
        "https://LINKEDIN.com/jobs/1",
        "linkedin.com/jobs/1",
        "https://lnkd.in/abc",
        "https://www.indeed.com/viewjob?jk=1",
        "https://ke.indeed.com/viewjob?jk=1",
        "https://www.glassdoor.com/job-listing/x",
        "https://www.facebook.com/jobs/1",
        "https://x.com/someone/status/1",
        "https://twitter.com/someone/status/1",
        "https://www.instagram.com/p/1",
        "https://www.tiktok.com/@a/video/1",
    ],
)
def test_blocked_sites_show_the_paste_instead_message(
    user, router: respx.MockRouter, url: str, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BlockedSite) as info:
        manual.add_job_from_url(user, url)
    assert (
        str(info.value)
        == "This site can't be read automatically. Paste the job description instead."
    )
    assert info.value.reason == "blocked_site" and fake_llm.calls == [] and Job.objects.count() == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://notlinkedin.com/jobs",
        "https://linkedin.com.evil.example/x",
        "https://example.com/linkedin.com",
    ],
)
def test_lookalike_domains_are_not_blocked(user, router: respx.MockRouter, url: str) -> None:  # type: ignore[no-untyped-def]
    for path in ("/jobs", "/x", "/linkedin.com"):
        serve(router, path, JSONLD_PAGE)
    assert manual.add_job_from_url(user, url).job.title == "Data Annotator"


def test_a_short_link_that_redirects_to_a_blocked_site_is_refused(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    router.get(f"https://{PUBLIC}/abc").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/jobs/view/1"}
        )
    )
    with pytest.raises(BlockedSite):
        manual.add_job_from_url(user, "https://short.example/abc")
    assert Job.objects.count() == 0


# --- unsafe URLs ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://127.0.0.1:80/secret",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://2130706433/",
        "ftp://example.com/job",
        "file:///etc/passwd",
        "https://user:pw@example.com/job",
        "https://example.com:22/job",
    ],
)
def test_private_and_unsafe_urls_are_rejected_without_any_request(
    user, router: respx.MockRouter, url: str, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    route = router.get(url__startswith="http").mock(
        return_value=httpx.Response(200, text=JSONLD_PAGE)
    )
    with pytest.raises(ManualJobError) as info:
        manual.add_job_from_url(user, url)
    assert info.value.reason in ("unsafe_url", "bad_url")
    assert not route.called and Job.objects.count() == 0
    if info.value.reason == "unsafe_url":
        (line,) = handler.events("ssrf_blocked")
        assert line["level"] == "WARNING" and url not in "".join(
            handler.lines
        )  # reason only, never the URL


def test_a_name_that_resolves_to_localhost_is_rejected(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    DNS["rebind.example"] = ["127.0.0.1"]
    route = router.get(url__startswith="http").mock(
        return_value=httpx.Response(200, text=JSONLD_PAGE)
    )
    with pytest.raises(ManualJobError, match="paste the job description"):
        manual.add_job_from_url(user, "https://rebind.example/job")
    assert not route.called


def test_a_redirect_to_an_internal_address_is_rejected(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    router.get(f"https://{PUBLIC}/go").mock(
        return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )
    )
    secret = router.get(url__regex=r"http://169").mock(
        return_value=httpx.Response(200, text="SECRET")
    )
    with pytest.raises(ManualJobError):
        manual.add_job_from_url(user, "https://redirector.example/go")
    assert not secret.called


def test_dns_that_changes_after_the_first_check_is_caught_on_the_next_hop(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    DNS["second.example"] = ["127.0.0.1"]
    router.get(f"https://{PUBLIC}/go").mock(
        return_value=httpx.Response(302, headers={"location": "https://second.example/x"})
    )
    with pytest.raises(ManualJobError):
        manual.add_job_from_url(user, "https://first.example/go")


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (httpx.Response(404), "could not be loaded"),
        (httpx.Response(500), "could not be loaded"),
        (
            httpx.Response(
                200, content=b"x" * (ssrf.MAX_BYTES + 1), headers={"content-type": "text/html"}
            ),
            "too large",
        ),
        (
            httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
            "not a web page",
        ),
        (httpx.ConnectError("refused"), "could not reach"),
    ],
)
def test_fetch_problems_become_friendly_messages(
    user, router: respx.MockRouter, response, fragment: str
) -> None:  # type: ignore[no-untyped-def]
    route = router.get(f"https://{PUBLIC}/job")
    route.mock(side_effect=response) if isinstance(response, Exception) else route.mock(
        return_value=response
    )
    with pytest.raises(ManualJobError) as info:
        manual.add_job_from_url(user, "https://jobs.example/job")
    assert fragment in info.value.message and Job.objects.count() == 0


# --- path 1: a configured source's own parser ------------------------------------------------------------------------


def test_an_expertwoka_url_uses_the_expertwoka_fetcher(
    user, router: respx.MockRouter, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    seed_sources()
    route = router.get("https://expertwoka.com/opportunities/finance-expert-afterquery").mock(
        return_value=httpx.Response(200, text=saved_page("detail_afterquery_us_jsonld.html"))
    )
    with django_capture_on_commit_callbacks(execute=True):
        result = manual.add_job_from_url(
            user,
            "https://expertwoka.com/opportunities/finance-expert-afterquery?utm_source=newsletter",
        )

    job = result.job
    assert (result.created, result.path) == (True, "known_source")
    assert (job.source.name, job.origin, job.visibility, job.owner) == (
        "expertwoka",
        "scraped",
        "public",
        None,
    )
    assert (job.title, job.company, job.external_id) == (
        "Finance Expert",
        "AfterQuery",
        "finance-expert-afterquery",
    )
    assert route.call_count == 1
    assert (
        extract_calls() == 0 and not LLMCall.objects.filter(user=user).exists()
    )  # nothing charged to the user
    assert (
        LLMCall.objects.filter(task="extract_job", user__isnull=True).count() == 1
    )  # system-paid structure


def test_a_known_job_is_reused_without_fetching(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    seed_sources()
    from apps.sources.models import Source

    existing = JobFactory(
        source=Source.objects.get(name="expertwoka"), external_id="finance-expert-afterquery"
    )
    route = router.get(url__startswith="https://expertwoka.com").mock(
        return_value=httpx.Response(200, text="x")
    )
    result = manual.add_job_from_url(
        user, "https://expertwoka.com/opportunities/finance-expert-afterquery"
    )
    assert (result.job, result.created, result.path) == (
        existing,
        False,
        "existing",
    ) and not route.called


def test_a_source_site_page_that_is_not_a_job_page_falls_through(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    seed_sources()
    serve(router, "/about", PLAIN_PAGE)
    result = manual.add_job_from_url(user, "https://expertwoka.com/about")
    assert result.path == "llm"  # not an /opportunities/ link, so the generic reader handles it


def test_a_source_fetch_failure_is_a_friendly_error(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    seed_sources()
    router.get("https://expertwoka.com/opportunities/gone-job").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(ManualJobError, match="could not read that listing"):
        manual.add_job_from_url(user, "https://expertwoka.com/opportunities/gone-job")


def test_disabled_sources_are_not_used(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    seed_sources()
    from apps.sources.models import Source

    Source.objects.update(enabled=False)
    serve(router, "/opportunities/some-job-acme", JSONLD_PAGE)
    assert (
        manual.add_job_from_url(user, "https://expertwoka.com/opportunities/some-job-acme").path
        == "jsonld"
    )


# --- path 2: JSON-LD, no LLM -----------------------------------------------------------------------------------------


def test_a_page_with_jsonld_is_parsed_without_the_llm(
    user, router: respx.MockRouter, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    route = serve(router, "/careers/annotator", JSONLD_PAGE)
    with django_capture_on_commit_callbacks(execute=True):
        result = manual.add_job_from_url(
            user, "https://labelco.example/careers/annotator?utm_campaign=x"
        )
    job = result.job
    assert (result.created, result.path) == (True, "jsonld")
    assert (job.origin, job.visibility, job.source, job.owner) == (
        "manual_url",
        "public",
        None,
        None,
    )
    assert (job.title, job.company, job.work_type) == ("Data Annotator", "LabelCo", "remote")
    assert (job.pay_max, job.pay_period, job.eligible_countries) == (Decimal(15), "hour", ["KE"])
    assert job.url == "https://labelco.example/careers/annotator"  # tracking parameters removed
    assert route.call_count == 1
    assert extract_calls() == 0  # the user's page-extraction allowance is untouched
    assert all(call["task"] != "extract_page" for call in fake_llm.calls)
    assert LLMCall.objects.filter(task="extract_job", user__isnull=True).count() == 1


def test_the_same_link_twice_reuses_the_job_and_makes_no_request(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    route = serve(router, "/careers/annotator", JSONLD_PAGE)
    first = manual.add_job_from_url(user, "https://labelco.example/careers/annotator")
    second = manual.add_job_from_url(user, "labelco.example/careers/annotator?utm_source=again#top")
    assert second.job == first.job and second.created is False and second.path == "existing"
    assert route.call_count == 1 and Job.objects.count() == 1


def test_the_same_job_at_a_different_url_is_deduplicated(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    serve(router, "/a", JSONLD_PAGE)
    serve(router, "/b", JSONLD_PAGE)
    first = manual.add_job_from_url(user, "https://one.example/a")
    second = manual.add_job_from_url(UserFactory(), "https://two.example/b")
    assert second.job == first.job and not second.created and Job.objects.count() == 1


def test_a_public_job_added_by_one_user_is_visible_to_another(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    serve(router, "/a", JSONLD_PAGE)
    job = manual.add_job_from_url(user, "https://one.example/a").job
    assert job in Job.objects.visible_to(UserFactory())


def test_an_invalid_jsonld_posting_is_not_a_job(
    user, router: respx.MockRouter, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    page = '<script type="application/ld+json">{"@type": "JobPosting"}</script>'
    serve(router, "/x", page)
    with pytest.raises(ManualJobError, match="does not look like a job posting"):
        manual.add_job_from_url(user, "https://t.example/x")


# --- path 3: LLM extraction on the user's quota ------------------------------------------------------------------------


def test_an_arbitrary_page_uses_extract_page(
    user, router: respx.MockRouter, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    serve(router, "/jobs/support", PLAIN_PAGE)
    with django_capture_on_commit_callbacks(execute=True):
        result = manual.add_job_from_url(user, "https://small.example/jobs/support")
    job = result.job
    assert (result.created, result.path) == (True, "llm")
    assert (job.title, job.company, job.origin, job.visibility) == (
        "Support Engineer",
        "Example Co",
        "manual_url",
        "public",
    )
    assert (job.pay_max, job.eligible_countries, job.work_type) == (20, ["KE"], "remote")
    call = LLMCall.objects.get(task="extract_page")
    assert call.user == user and call.success and not call.cache_hit  # counts for the user
    sent = fake_llm.calls[0]
    assert (
        "<page>" in sent["messages"][0]["content"] and "Home" not in sent["messages"][0]["content"]
    )  # nav stripped


def test_manual_extractions_count_against_the_monthly_allowance(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    UsageQuota.objects.create(user=user, monthly_manual_extractions=1)
    serve(router, "/one", PLAIN_PAGE)
    serve(router, "/two", PLAIN_PAGE.replace("Example Co", "Other Co"))
    manual.add_job_from_url(user, "https://a.example/one")
    with pytest.raises(QuotaExceeded) as info:
        manual.add_job_from_url(user, "https://b.example/two")
    assert info.value.scope == "manual" and Job.objects.count() == 1


def test_the_same_page_content_is_answered_from_the_cache(
    user, router: respx.MockRouter, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    serve(router, "/p", PLAIN_PAGE)
    manual.add_job_from_url(user, "https://a.example/p")
    Job.objects.all().delete()
    manual.add_job_from_url(UserFactory(), "https://a.example/p")
    assert len(fake_llm.calls) == 1  # the second reader did not pay or call the model


@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        ('{"is_job_posting": false}', "single job posting"),
        ('{"is_job_posting": true, "title": ""}', "single job posting"),
    ],
)
def test_pages_that_are_not_jobs_are_refused(
    user, router: respx.MockRouter, fake_llm: FakeProvider, answer: str, fragment: str
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(answer)
    serve(router, "/blog", PLAIN_PAGE)
    with pytest.raises(ManualJobError, match=fragment):
        manual.add_job_from_url(user, "https://a.example/blog")
    assert Job.objects.count() == 0


def test_an_empty_page_is_refused_before_the_llm(
    user, router: respx.MockRouter, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    serve(router, "/e", "<html><body><nav>x</nav></body></html>")
    with pytest.raises(ManualJobError, match="readable text"):
        manual.add_job_from_url(user, "https://a.example/e")
    assert fake_llm.calls == []


def test_llm_failures_are_friendly(user, router: respx.MockRouter, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["junk", "junk"])
    serve(router, "/p", PLAIN_PAGE)
    with pytest.raises(ManualJobError, match="paste the description instead"):
        manual.add_job_from_url(user, "https://a.example/p")


def test_company_falls_back_to_the_site_name(
    user, router: respx.MockRouter, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append('{"is_job_posting": true, "title": "Rater", "company": ""}')
    serve(router, "/p", PLAIN_PAGE)
    assert (
        manual.add_job_from_url(user, "https://careers.example/p").job.company == "careers.example"
    )


# --- paste a description -----------------------------------------------------------------------------------------------


def test_pasted_text_makes_a_private_job_owned_by_the_user(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(
        json.dumps(
            {
                "title": "Customer Support Agent",
                "company": "Acme",
                "summary": "Answer customer emails.",
                "seniority": "junior",
                "required_skills": ["Writing"],
                "nice_to_have_skills": [],
                "responsibilities": [],
                "requirements": [],
                "pay_text": "$12 per hour",
                "locations": ["Kenya"],
                "work_type": "remote",
            }
        )
    )
    result = manual.add_job_from_text(user, TEXT, url="https://acme.example/jobs/1?utm_source=x")
    job = result.job
    assert (result.created, result.path) == (True, "text")
    assert (job.origin, job.visibility, job.owner, job.source) == (
        "manual_text",
        "private",
        user,
        None,
    )
    assert (job.title, job.company, job.url) == (
        "Customer Support Agent",
        "Acme",
        "https://acme.example/jobs/1",
    )
    assert job.description_text == TEXT and job.description_html == ""
    assert (job.pay_max, job.pay_period, job.eligible_countries, job.work_type) == (
        12,
        "hour",
        ["KE"],
        "remote",
    )
    assert job.extracted["summary"] == "Answer customer emails." and job.extracted_at
    call = LLMCall.objects.get()
    assert (
        call.task == "extract_job" and call.user == user
    )  # the user's allowance, not the system's


def test_the_users_own_title_and_company_win_and_are_sent_to_the_model(
    user, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    result = manual.add_job_from_text(user, TEXT, title="  My Title ", company=" My Co ")
    assert (result.job.title, result.job.company) == ("My Title", "My Co")
    content = fake_llm.calls[0]["messages"][0]["content"]
    assert (
        "Title: My Title" in content and "Company: My Co" in content and "<job_posting>" in content
    )


def test_missing_title_and_company_are_asked_for(
    user, fake_llm: FakeProvider
) -> None:  # the fixture answer names neither
    with pytest.raises(NeedsDetails) as info:
        manual.add_job_from_text(user, TEXT)
    assert info.value.missing == ["title", "company"] and Job.objects.count() == 0
    again = manual.add_job_from_text(
        user, TEXT, title="Agent", company="Acme"
    )  # resubmitted with details
    assert again.created and len(fake_llm.calls) == 1  # the extraction was cached: no second charge


def test_only_the_missing_part_is_requested(user, fake_llm: FakeProvider) -> None:
    fake_llm.script.append(
        json.dumps(
            {
                "title": "Agent",
                "company": None,
                "summary": "x",
                "seniority": "unknown",
                "required_skills": [],
                "nice_to_have_skills": [],
                "responsibilities": [],
                "requirements": [],
            }
        )
    )
    with pytest.raises(NeedsDetails) as info:
        manual.add_job_from_text(user, TEXT)
    assert info.value.missing == ["company"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [("short", "too_short"), ("x" * 79, "too_short"), ("x" * 30_001, "too_long")],
)
def test_text_length_limits(user, text: str, reason: str, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ManualJobError) as info:
        manual.add_job_from_text(user, text, title="T", company="C")
    assert info.value.reason == reason and fake_llm.calls == []


@pytest.mark.parametrize(
    "link", ["ftp://x.test/a", "javascript:alert(1)", "not a url", "acme.example/jobs"]
)
def test_the_optional_link_must_be_http(user, link: str, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ManualJobError) as info:
        manual.add_job_from_text(user, TEXT, title="T", company="C", url=link)
    assert info.value.reason == "bad_link" and fake_llm.calls == []


def test_the_pasted_link_is_never_fetched(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    route = router.get(url__startswith="http").mock(return_value=httpx.Response(200, text="x"))
    manual.add_job_from_text(user, TEXT, title="T", company="C", url="http://127.0.0.1/admin")
    assert not route.called  # it is only stored as a reference


def test_pasting_the_same_text_again_returns_the_same_job_without_a_charge(
    user, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    first = manual.add_job_from_text(user, TEXT, title="T", company="C")
    second = manual.add_job_from_text(user, "  " + TEXT + "\n", title="Different", company="Other")
    assert second.job == first.job and not second.created and second.path == "existing"
    assert len(fake_llm.calls) == 1 and Job.objects.count() == 1


def test_another_user_pasting_the_same_text_gets_their_own_private_job(
    user, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    other = UserFactory()
    mine = manual.add_job_from_text(user, TEXT, title="T", company="C").job
    theirs = manual.add_job_from_text(other, TEXT, title="T", company="C").job
    assert mine != theirs and (mine.owner, theirs.owner) == (user, other)
    assert mine not in Job.objects.visible_to(other) and theirs not in Job.objects.visible_to(user)
    assert len(fake_llm.calls) == 2  # per-user cache keys: nothing leaks between users


def test_pasted_text_is_not_deduplicated_against_public_jobs(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    public = JobFactory(title="Data Annotator", company="LabelCo", source=None, origin="manual_url")
    mine = manual.add_job_from_text(user, TEXT, title="Data Annotator", company="LabelCo").job
    assert mine != public and mine.visibility == "private" and mine.duplicate_of is None


def test_pasted_text_counts_against_the_monthly_allowance(user) -> None:  # type: ignore[no-untyped-def]
    UsageQuota.objects.create(user=user, monthly_manual_extractions=1)
    manual.add_job_from_text(user, TEXT, title="T", company="C")
    with pytest.raises(QuotaExceeded):
        manual.add_job_from_text(user, TEXT + " More text added here.", title="T2", company="C2")


def test_llm_failure_on_pasted_text_is_friendly(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["junk", "junk"])
    with pytest.raises(ManualJobError, match="try again"):
        manual.add_job_from_text(user, TEXT, title="T", company="C")
    assert Job.objects.count() == 0


def test_scam_signals_in_pasted_text_are_flagged(user) -> None:  # type: ignore[no-untyped-def]
    text = (
        TEXT
        + " A small registration fee is required before you start, then WhatsApp us on +254700000000."
    )
    job = manual.add_job_from_text(user, text, title="T", company="C").job
    assert {"asks_for_payment", "messaging_app_contact"} <= set(job.scam_flags)


def test_logs_never_contain_the_pasted_text_or_urls(
    user, router: respx.MockRouter, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    manual.add_job_from_text(user, TEXT + " SECRET-PASTE-MARKER", title="T", company="C")
    serve(router, "/p", JSONLD_PAGE)
    manual.add_job_from_url(user, "https://secret-host.example/p?token=SECRET-TOKEN")
    output = "".join(handler.lines)
    for secret in ("SECRET-PASTE-MARKER", "secret-host.example", "SECRET-TOKEN", "jobs@acme.test"):
        assert secret not in output
    assert handler.events("manual_job_added")


# --- the matching hook ---------------------------------------------------------------------------------------------------


def test_the_matching_hook_is_a_noop_until_phase_6(user) -> None:  # type: ignore[no-untyped-def]
    assert matching_hook.job_added_for_user(user, JobFactory()) is False


def test_the_matching_hook_calls_the_matching_service_when_it_exists(
    user, monkeypatch: pytest.MonkeyPatch, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(matching_hook.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        matching_hook.importlib, "import_module",
        lambda name: SimpleNamespace(match_job_for_user=lambda u, j: calls.append((u, j))),
    )  # fmt: skip
    result = manual.add_job_from_text(user, TEXT, title="T", company="C")
    assert calls == [(user, result.job)]


# --- the pages ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(user) -> Client:  # type: ignore[no-untyped-def]
    client = Client()
    client.force_login(user)
    return client


def test_pages_require_login() -> None:
    for url in ("/app/jobs/add/", "/app/jobs/1/"):
        assert "/accounts/login/" in Client().get(url)["Location"]
    for url in ("/app/jobs/add/url/", "/app/jobs/add/text/"):
        assert Client().post(url, {})["Location"].startswith("/accounts/login/")


def test_add_page_has_two_tabs_and_a_form_each(client: Client) -> None:
    url_tab = client.get("/app/jobs/add/").content.decode()
    assert (
        'aria-selected="true"' in url_tab
        and "Paste a link" in url_tab
        and 'action="/app/jobs/add/url/"' in url_tab
    )
    assert "LinkedIn" in url_tab and 'action="/app/jobs/add/text/"' not in url_tab
    text_tab = client.get("/app/jobs/add/?tab=text").content.decode()
    assert (
        'action="/app/jobs/add/text/"' in text_tab
        and "Only you can see jobs you paste in" in text_tab
    )


def test_the_nav_links_to_the_add_page(client: Client) -> None:
    html = client.get("/app/settings/account/").content.decode()
    assert 'href="/app/jobs/add/"' in html and ">Add a job<" in html


def test_adding_by_link_redirects_to_the_job_page(client: Client, router: respx.MockRouter) -> None:
    serve(router, "/careers/annotator", JSONLD_PAGE)
    response = client.post(
        "/app/jobs/add/url/", {"url": "https://labelco.example/careers/annotator"}
    )
    job = Job.objects.get()
    assert response["Location"] == f"/app/jobs/{job.pk}/"
    page = client.get(response["Location"]).content.decode()
    assert "Data Annotator" in page and "LabelCo" in page and "Open to people in KE" in page
    assert "USD 10-15 per hour" in page and "Label images for AI training." in page


def test_the_second_add_of_a_link_says_we_already_had_it(
    client: Client, router: respx.MockRouter
) -> None:
    serve(router, "/a", JSONLD_PAGE)
    client.post("/app/jobs/add/url/", {"url": "https://x.example/a"})
    response = client.post("/app/jobs/add/url/", {"url": "https://x.example/a"}, follow=True)
    assert "We already had this job" in response.content.decode()


def test_blocked_and_unsafe_links_show_errors_on_the_form(client: Client) -> None:
    blocked = client.post("/app/jobs/add/url/", {"url": "https://www.linkedin.com/jobs/view/1"})
    assert (
        blocked.status_code == 400
        and "Paste the job description instead" in blocked.content.decode()
    )
    unsafe = client.post("/app/jobs/add/url/", {"url": "http://127.0.0.1/admin"})
    assert unsafe.status_code == 400 and "cannot be used" in unsafe.content.decode()
    assert Job.objects.count() == 0


def test_quota_exceeded_shows_a_friendly_message(
    client: Client, user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    UsageQuota.objects.create(user=user, monthly_manual_extractions=0)
    serve(router, "/p", PLAIN_PAGE)
    response = client.post("/app/jobs/add/url/", {"url": "https://a.example/p"})
    assert response.status_code == 400 and "manual job extractions" in response.content.decode()


def test_adding_by_text_and_the_needs_details_flow(client: Client, user) -> None:  # type: ignore[no-untyped-def]
    first = client.post("/app/jobs/add/text/", {"text": TEXT})
    html = first.content.decode()
    assert first.status_code == 400 and "could not tell the job title or company" in html
    assert html.count("Please fill this in.") == 2 and TEXT[:40] in html  # the text is kept

    second = client.post("/app/jobs/add/text/", {"text": TEXT, "title": "Agent", "company": "Acme"})
    job = Job.objects.get()
    assert second["Location"] == f"/app/jobs/{job.pk}/" and job.owner == user
    assert "Private: only you can see this" in client.get(second["Location"]).content.decode()
    assert (
        "Agent" in client.get("/app/jobs/add/?tab=text").content.decode()
    )  # listed under "Jobs you pasted"


def test_text_form_errors(client: Client) -> None:
    short = client.post("/app/jobs/add/text/", {"text": "too short", "title": "T", "company": "C"})
    assert short.status_code == 400 and "too short" in short.content.decode()
    bad_link = client.post(
        "/app/jobs/add/text/", {"text": TEXT, "title": "T", "company": "C", "url": "ftp://x"}
    )
    assert "must start with http" in bad_link.content.decode()
    assert client.post("/app/jobs/add/text/", {}).status_code == 400


def test_the_daily_limit_is_shared_across_both_tabs(client: Client) -> None:
    codes = [
        client.post("/app/jobs/add/url/", {"url": "http://127.0.0.1/"}).status_code
        for _ in range(10)
    ]
    codes += [client.post("/app/jobs/add/text/", {"text": "short"}).status_code for _ in range(10)]
    assert 429 not in codes
    over = client.post("/app/jobs/add/url/", {"url": "http://127.0.0.1/"})
    assert over.status_code == 429 and "slow down" in over.content.decode().lower()
    assert client.post("/app/jobs/add/text/", {"text": "short"}).status_code == 429
    assert client.get("/app/jobs/add/").status_code == 200  # only additions are limited


def test_the_limit_is_per_user(client: Client) -> None:
    for _ in range(21):
        client.post("/app/jobs/add/url/", {"url": "http://127.0.0.1/"})
    other = Client()
    other.force_login(UserFactory())
    assert other.post("/app/jobs/add/url/", {"url": "http://127.0.0.1/"}).status_code == 400


def test_manual_extractions_show_on_the_usage_page(
    client: Client, router: respx.MockRouter
) -> None:
    serve(router, "/p", PLAIN_PAGE)
    client.post("/app/jobs/add/url/", {"url": "https://a.example/p"})
    client.post("/app/jobs/add/text/", {"text": TEXT, "title": "T", "company": "C"})
    assert "2 of 60 used" in client.get("/app/settings/usage/").content.decode()


# --- the job page and isolation ----------------------------------------------------------------------------------------------


def test_a_private_job_is_visible_only_to_its_owner(client: Client, user) -> None:  # type: ignore[no-untyped-def]
    job = manual.add_job_from_text(user, TEXT, title="Secret Role", company="Secret Co").job
    assert "Secret Role" in client.get(f"/app/jobs/{job.pk}/").content.decode()

    stranger = Client()
    stranger.force_login(UserFactory())
    assert stranger.get(f"/app/jobs/{job.pk}/").status_code == 404
    assert "Secret Role" not in stranger.get("/app/jobs/add/?tab=text").content.decode()
    assert "/accounts/login/" in Client().get(f"/app/jobs/{job.pk}/")["Location"]


def test_public_jobs_are_visible_to_every_user(client: Client) -> None:
    job = JobFactory(title="Public Role")
    assert "Public Role" in client.get(f"/app/jobs/{job.pk}/").content.decode()


def test_a_job_of_another_user_cannot_be_reached_through_the_recent_list(
    client: Client, user
) -> None:  # type: ignore[no-untyped-def]
    other = UserFactory()
    theirs = manual.add_job_from_text(other, TEXT, title="Their Role", company="C").job
    assert "Their Role" not in client.get("/app/jobs/add/?tab=text").content.decode()
    assert client.get(f"/app/jobs/{theirs.pk}/").status_code == 404


def test_a_duplicate_redirects_to_the_canonical_job(client: Client) -> None:
    original = JobFactory()
    duplicate = JobFactory(duplicate_of=original)
    assert client.get(f"/app/jobs/{duplicate.pk}/")["Location"] == f"/app/jobs/{original.pk}/"


@pytest.mark.parametrize(
    ("kwargs", "badge"),
    [
        ({"is_global": True, "eligibility_known": True}, "Open to people in KE"),
        ({"eligible_countries": ["US"], "eligibility_known": True}, "Not open to people in KE"),
        ({"eligible_regions": ["AFRICA"], "eligibility_known": True}, "Open to people in KE"),
        ({"eligibility_known": False}, "Eligibility unclear"),
    ],
)
def test_the_eligibility_badge(client: Client, kwargs: dict[str, object], badge: str) -> None:
    job = JobFactory(**kwargs)
    assert badge in client.get(f"/app/jobs/{job.pk}/").content.decode()


def test_the_badge_uses_the_users_country(client: Client, user) -> None:  # type: ignore[no-untyped-def]
    UserSettings.objects.filter(user=user).update(country="US")
    job = JobFactory(eligible_countries=["US"], eligibility_known=True)
    assert "Open to people in US" in client.get(f"/app/jobs/{job.pk}/").content.decode()


def test_scam_flags_and_expiry_are_shown(client: Client) -> None:
    job = JobFactory(scam_flags=["asks_for_payment"], status="expired")
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "Be careful with this listing" in html and "Asks you to pay a fee" in html
    assert "This job may have closed" in html


def test_descriptions_are_sanitised_or_escaped(client: Client, user) -> None:  # type: ignore[no-untyped-def]
    pasted = manual.add_job_from_text(
        user, TEXT + " <script>alert('x')</script> <b>bold</b>", title="T", company="C"
    ).job
    html = client.get(f"/app/jobs/{pasted.pk}/").content.decode()
    assert "<script>alert" not in html and "&lt;script&gt;" in html  # text is escaped

    public = JobFactory(
        description_html='<p>ok</p><script>bad()</script><a href="javascript:x()">l</a>',
        description_text="ok",
    )
    from apps.core.sanitize import clean_html

    Job.objects.filter(pk=public.pk).update(
        description_html=clean_html(public.description_html)
    )  # as ingestion does
    html = client.get(f"/app/jobs/{public.pk}/").content.decode()
    assert "<script>bad" not in html and "javascript:" not in html and "<p>ok</p>" in html


def test_the_original_posting_link_is_safe(client: Client) -> None:
    job = JobFactory(url="https://x.example/job", apply_url="https://apply.example/here")
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert (
        'href="https://apply.example/here"' in html and 'rel="noopener noreferrer nofollow"' in html
    )


def test_the_summary_from_extraction_is_shown(client: Client) -> None:
    job = JobFactory(
        extracted={
            "summary": "Rate AI answers.",
            "required_skills": ["Writing"],
            "requirements": ["10 hours"],
        }
    )
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "Rate AI answers." in html and "Skills asked for" in html and "10 hours" in html
