"""Alert-email parsers, mailbox parsing and routing (spec §9.2, §9.4)."""

import json

import httpx
import pytest
import respx

from apps.inbound import services
from apps.inbound.mailbox import PROCESSED, UNROUTED, FakeMailbox, MailMessage, parse_message
from apps.inbound.parsers import MAX_JOBS_PER_EMAIL, choose_parser
from apps.inbound.parsers.generic import GenericAlertParser, collect_links
from apps.inbound.parsers.linkedin import LinkedInAlertParser
from apps.jobs.models import Job
from apps.llm.models import LLMCall
from apps.llm.providers.fake import FakeProvider
from apps.sources.services import seed_sources
from tests.factories import UserFactory
from tests.inbound_helpers import load_eml
from tests.source_helpers import page as saved_page

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():  # type: ignore[no-untyped-def]
    return UserFactory()


# --- reading a message ---------------------------------------------------------------------------------


def test_a_message_is_parsed_into_headers_and_both_bodies(user) -> None:  # type: ignore[no-untyped-def]
    message = load_eml("linkedin_alert.eml", user)
    assert message.from_address == "jobalerts-noreply@linkedin.com"
    assert message.message_id == "linkedin-alert-0001@mail.linkedin.example"
    assert message.subject == "3 new jobs for Customer Support Agent in Kenya"
    assert "View job" in message.text and "<table" in message.html
    assert services.ensure_address(user).address in message.recipients
    assert message.received_at.isoformat() == "2026-03-04T06:30:00+00:00"


def test_a_message_without_a_date_or_id_still_parses() -> None:
    message = parse_message(b"From: a@b.example\nTo: c@d.example\nSubject: x\n\nbody\n")
    assert message.message_id == "" and message.received_at.tzinfo is not None
    assert message.text.strip() == "body"


def test_html_only_messages_have_no_text_part() -> None:
    raw = b"From: a@b.example\nSubject: x\nContent-Type: text/html\n\n<p>hi</p>\n"
    message = parse_message(raw)
    assert message.text == "" and "<p>hi</p>" in message.html


# --- LinkedIn ---------------------------------------------------------------------------------------------


def test_the_linkedin_parser_claims_linkedin_senders_only() -> None:
    parser = LinkedInAlertParser()
    for sender, expected in (
        ("jobalerts-noreply@linkedin.com", True),
        ("jobs-listings@linkedin.com", True),
        ("x@e.linkedin.com", True),
        ("x@linkedin.com.evil.example", False),
        ("x@notlinkedin.com", False),
        ("", False),
    ):
        message = MailMessage(
            "1", "id", sender, "s", parse_message(b"Subject: x\n\n").received_at, []
        )
        assert parser.matches(message) is expected


def test_a_linkedin_html_alert_gives_every_job_with_title_company_and_location(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    result = LinkedInAlertParser().parse(load_eml("linkedin_alert.eml", user), user)
    assert [(j.title, j.company, j.locations) for j in result.jobs] == [
        ("Customer Support Agent", "Acme Ltd", ["Nairobi, Kenya (Remote)"]),
        ("IT Helpdesk Technician", "Beta Networks", ["Mombasa, Kenya"]),
        ("Remote Data Annotator", "Gamma Co", ["Kenya"]),
    ]
    assert [j.external_id for j in result.jobs] == [
        "linkedin:3912345678", "linkedin:3912345999", "linkedin:3912346111",
    ]  # fmt: skip
    assert [j.url for j in result.jobs] == [
        f"https://www.linkedin.com/jobs/view/{i}"
        for i in ("3912345678", "3912345999", "3912346111")
    ]
    assert [j.work_type for j in result.jobs] == ["remote", "", ""]
    assert len(router.calls) == 0  # no LinkedIn page, or any page, was fetched


def test_noise_lines_are_not_mistaken_for_company_or_location(user) -> None:  # type: ignore[no-untyped-def]
    jobs = LinkedInAlertParser().parse(load_eml("linkedin_alert.eml", user), user).jobs
    for job in jobs:
        for text in (job.company, *job.locations):
            assert text.lower() not in {
                "actively recruiting",
                "view job",
                "2 days ago",
                "be an early applicant",
            }


def test_a_text_only_linkedin_alert_is_read_too(user) -> None:  # type: ignore[no-untyped-def]
    jobs = LinkedInAlertParser().parse(load_eml("linkedin_alert_text_only.eml", user), user).jobs
    assert [(j.title, j.company, j.locations, j.work_type) for j in jobs] == [
        ("Technical Support Engineer", "Delta Cloud", ["Nairobi, Kenya"], ""),
        ("Virtual Assistant", "Epsilon Services", ["Remote"], "remote"),
    ]
    assert jobs[0].external_id == "linkedin:4000000001"


def test_an_alert_with_no_job_links_gives_nothing(user) -> None:  # type: ignore[no-untyped-def]
    message = load_eml("newsletter.eml", user)
    assert LinkedInAlertParser().parse(message, user).jobs == []


def test_the_same_job_linked_twice_is_one_job(user) -> None:  # type: ignore[no-untyped-def]
    jobs = LinkedInAlertParser().parse(load_eml("linkedin_alert.eml", user), user).jobs
    assert len({j.external_id for j in jobs}) == len(jobs)


def test_an_email_cannot_make_a_job_with_a_foreign_link(user) -> None:  # type: ignore[no-untyped-def]
    raw = (
        "From: x@linkedin.com\nTo: a@b.example\nSubject: s\nContent-Type: text/html\n\n"
        '<a href="https://evil.example/jobs/view/1234567890">Fake Job</a><p>Evil Co</p><p>Nowhere</p>'
    )
    message = parse_message(raw.encode())
    assert LinkedInAlertParser().parse(message, user).jobs == []


# --- the registry -------------------------------------------------------------------------------------------------


def test_the_registry_picks_linkedin_then_falls_back_to_generic(user) -> None:  # type: ignore[no-untyped-def]
    assert choose_parser(load_eml("linkedin_alert.eml", user)).name == "linkedin"
    assert choose_parser(load_eml("generic_alert.eml", user)).name == "generic"
    assert choose_parser(load_eml("newsletter.eml", user)).name == "generic"


# --- the generic parser ---------------------------------------------------------------------------------------------


def llm_reply(**override: object) -> str:
    body = {
        "is_job_alert": True,
        "jobs": [
            {"title": "Customer Care Representative", "company": "Zeta Telecom", "location": "Nairobi",
             "url": "https://jobsite.example.com/jobs/care-rep-123", "pay_text": "KES 60,000 a month", "summary": ""},
            {"title": "Support Team Lead", "company": "Eta Bank", "location": "Kisumu",
             "url": "https://jobsite.example.com/jobs/team-lead-456"},
        ],
    }  # fmt: skip
    body.update(override)
    return json.dumps(body)


def test_the_generic_parser_lists_jobs_with_the_model_system_paid(
    user, fake_llm: FakeProvider, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(llm_reply())
    result = GenericAlertParser().parse(load_eml("generic_alert.eml", user), user)
    assert [(j.title, j.company, j.pay_text) for j in result.jobs] == [
        ("Customer Care Representative", "Zeta Telecom", "KES 60,000 a month"),
        ("Support Team Lead", "Eta Bank", ""),
    ]
    assert result.jobs[0].url == "https://jobsite.example.com/jobs/care-rep-123"
    assert result.jobs[0].external_id.startswith("email:") and len(result.jobs[0].external_id) == 22
    call = LLMCall.objects.get()
    assert call.task == "extract_email_jobs" and call.user is None  # never charged to the user
    assert len(router.calls) == 0


def test_the_prompt_carries_the_email_fenced_as_untrusted_with_its_links(
    user, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(llm_reply())
    GenericAlertParser().parse(load_eml("generic_alert.eml", user), user)
    prompt = fake_llm.calls[0]["messages"][0]["content"]
    assert "<email>" in prompt and "Zeta Telecom" in prompt
    assert "https://jobsite.example.com/jobs/care-rep-123" in prompt


def test_a_link_the_model_invented_is_dropped(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    reply = json.loads(llm_reply())
    reply["jobs"][0]["url"] = "https://phishing.example.net/apply"
    fake_llm.script.append(json.dumps(reply))
    jobs = GenericAlertParser().parse(load_eml("generic_alert.eml", user), user).jobs
    assert jobs[0].url == "" and jobs[1].url == "https://jobsite.example.com/jobs/team-lead-456"


def test_a_newsletter_is_not_a_job_alert(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(json.dumps({"is_job_alert": False, "jobs": []}))
    assert GenericAlertParser().parse(load_eml("newsletter.eml", user), user).jobs == []


def test_jobs_without_a_title_or_company_are_skipped_and_the_number_is_capped(
    user, fake_llm: FakeProvider
) -> None:  # type: ignore[no-untyped-def]
    jobs = [{"title": "", "company": "X"}, {"title": "Y", "company": ""}]
    jobs += [{"title": f"Job {i}", "company": f"Co {i}"} for i in range(40)]
    fake_llm.script.append(json.dumps({"is_job_alert": True, "jobs": jobs}))
    parsed = GenericAlertParser().parse(load_eml("generic_alert.eml", user), user).jobs
    assert len(parsed) <= MAX_JOBS_PER_EMAIL and all(j.title and j.company for j in parsed)


def test_a_model_failure_gives_no_jobs_not_an_error(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["not json", "still not json"])
    assert GenericAlertParser().parse(load_eml("generic_alert.eml", user), user).jobs == []


def test_a_very_short_email_is_not_sent_to_the_model(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    message = parse_message(b"From: a@b.example\nSubject: hi\n\nok\n")
    assert GenericAlertParser().parse(message, user).jobs == [] and fake_llm.calls == []


def test_links_are_collected_once_from_html_and_text(user) -> None:  # type: ignore[no-untyped-def]
    links = collect_links(load_eml("linkedin_alert.eml", user))
    assert links.count("https://www.linkedin.com/comm/jobs/search/?keywords=support") == 1
    assert all(link.startswith("https://") for link in links)


def test_links_to_a_configured_source_are_read_through_that_source(
    user, fake_llm: FakeProvider, router: respx.MockRouter, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    seed_sources()
    router.get("https://expertwoka.com/opportunities/finance-expert-afterquery").mock(
        return_value=httpx.Response(200, text=saved_page("detail_afterquery_us_jsonld.html"))
    )
    raw = (
        "From: digest@expertwoka.example\nTo: a@b.example\nSubject: New role\n\n"
        "A new opportunity: https://expertwoka.com/opportunities/finance-expert-afterquery?utm=mail\n"
    )
    with django_capture_on_commit_callbacks(execute=True):
        result = GenericAlertParser().parse(parse_message(raw.encode()), user)
    assert [(j.source.name, j.external_id) for j in result.existing] == [
        ("expertwoka", "finance-expert-afterquery")
    ]
    assert result.jobs == [] and not LLMCall.objects.filter(task="extract_email_jobs").exists()


def test_a_known_job_link_is_reused_without_fetching(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    from apps.sources.models import Source
    from tests.factories import JobFactory

    seed_sources()
    known = JobFactory(
        source=Source.objects.get(name="expertwoka"), external_id="finance-expert-afterquery"
    )
    route = router.get(url__startswith="https://expertwoka.com").mock(
        return_value=httpx.Response(200)
    )
    raw = "From: a@b.example\nSubject: s\n\nhttps://expertwoka.com/opportunities/finance-expert-afterquery\n"
    result = GenericAlertParser().parse(parse_message(raw.encode()), user)
    assert result.existing == [known] and not route.called


# --- routing -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("header", ["To", "Delivered-To", "X-Forwarded-To", "X-Original-To"])
def test_the_address_is_found_in_any_routing_header(user, header: str) -> None:  # type: ignore[no-untyped-def]
    address = services.ensure_address(user).address
    raw = f"From: a@b.example\n{header}: Someone <{address.upper()}>\nSubject: s\n\nbody\n"
    message = parse_message(raw.encode())
    assert services.find_address(message.recipients).user == user  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "recipient",
    [
        "u-zzzzzz@alerts.example.test",  # no such token
        "u-aaaaaa@evil.example",  # right shape, wrong domain
        "someone@alerts.example.test",
        "u-@alerts.example.test",
        "",
    ],
)
def test_unknown_or_foreign_addresses_do_not_route(user, recipient: str) -> None:  # type: ignore[no-untyped-def]
    services.ensure_address(user)
    assert services.find_address([recipient]) is None


def test_an_inactive_address_or_user_does_not_route(user) -> None:  # type: ignore[no-untyped-def]
    address = services.ensure_address(user)
    address.active = False
    address.save()
    assert services.find_address([address.address]) is None
    address.active = True
    address.save()
    user.is_active = False
    user.save()
    assert services.find_address([address.address]) is None


def test_folders_are_named_as_in_the_spec() -> None:
    assert (PROCESSED, UNROUTED) == ("jobpilot/processed", "jobpilot/unrouted")
    box = FakeMailbox()
    box.finish("1", PROCESSED)
    assert box.finished == {"1": PROCESSED}
    assert Job.objects.count() == 0
