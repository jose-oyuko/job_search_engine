"""Polling the mailbox end to end with `.eml` fixtures and a fake mailbox (spec §9, §9.4)."""

import datetime
import json
import logging
import re
from types import SimpleNamespace
from typing import Any

import pytest
import respx

from apps.inbound import services
from apps.inbound.mailbox import PROCESSED, UNROUTED, ImapMailbox, parse_message
from apps.inbound.models import ForwardingConfirmation, InboundAddress, InboundEmail
from apps.jobs.models import Job
from apps.llm.models import LLMCall
from apps.llm.providers.fake import FakeProvider
from apps.matching.models import Match
from apps.notifications import services as notification_services
from apps.notifications.models import Notification
from tests.factories import UserFactory
from tests.inbound_helpers import load_eml, mailbox_with
from tests.matching_helpers import make_user
from tests.notif_helpers import make_notif_user, utc

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():  # type: ignore[no-untyped-def]
    return make_user(skills=("Helpdesk",), track=True, target_titles=["support", "helpdesk"])


def poll(*messages: Any) -> dict[str, int]:
    mailbox_with(*messages)
    return services.poll_mailbox()


# --- the happy path -------------------------------------------------------------------------------


def test_a_linkedin_alert_creates_the_jobs_without_fetching_linkedin(
    user, router: respx.MockRouter
) -> None:  # type: ignore[no-untyped-def]
    counts = poll(load_eml("linkedin_alert.eml", user))
    assert counts["messages"] == 1 and counts["parsed"] == 1 and counts["jobs"] == 3
    jobs = Job.objects.order_by("external_id")
    assert [(j.title, j.company) for j in jobs] == [
        ("Customer Support Agent", "Acme Ltd"),
        ("IT Helpdesk Technician", "Beta Networks"),
        ("Remote Data Annotator", "Gamma Co"),
    ]
    first = jobs[0]
    assert (first.origin, first.visibility, first.source, first.owner) == (
        "email",
        "public",
        None,
        None,
    )
    assert first.external_id == "linkedin:3912345678"
    assert first.url == "https://www.linkedin.com/jobs/view/3912345678"
    assert first.work_type == "remote" and first.locations_raw == ["Nairobi, Kenya (Remote)"]
    assert len(router.calls) == 0  # nothing at all was fetched


def test_the_receiving_user_gets_a_match_for_every_job(user) -> None:  # type: ignore[no-untyped-def]
    poll(load_eml("linkedin_alert.eml", user))
    matches = Match.objects.for_user(user)
    assert matches.count() == 3 and {m.job.origin for m in matches} == {"email"}
    assert all(m.scoring_queued_at is not None for m in matches)  # scoring is on its way


def test_the_email_is_recorded_and_filed_as_processed(user) -> None:  # type: ignore[no-untyped-def]
    message = load_eml("linkedin_alert.eml", user)
    box = mailbox_with(message)
    services.poll_mailbox()
    record = InboundEmail.objects.get()
    assert (record.user, record.parser, record.status, record.jobs_found) == (
        user,
        "linkedin",
        "parsed",
        3,
    )
    assert record.message_id == "linkedin-alert-0001@mail.linkedin.example"
    assert record.subject.startswith("3 new jobs") and "View job" in record.body_text
    assert box.finished == {"1": PROCESSED} and box.closed


def test_other_users_rule_filters_see_the_new_public_jobs(
    user, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    other = make_user(skills=("Helpdesk",), track=True, target_titles=["support", "helpdesk"])
    with django_capture_on_commit_callbacks(execute=True):
        poll(load_eml("linkedin_alert.eml", user))
    assert Match.objects.for_user(other).filter(job__title="Customer Support Agent").exists()
    assert Match.objects.for_user(user).count() == 3  # still one match each, no duplicates


def test_a_text_only_alert_works(user) -> None:  # type: ignore[no-untyped-def]
    assert poll(load_eml("linkedin_alert_text_only.eml", user))["jobs"] == 2
    assert set(Job.objects.values_list("external_id", flat=True)) == {
        "linkedin:4000000001",
        "linkedin:4000000002",
    }


# --- generic emails -----------------------------------------------------------------------------------


def test_a_generic_alert_goes_through_the_model(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(
        json.dumps(
            {
                "is_job_alert": True,
                "jobs": [{"title": "Customer Care Representative", "company": "Zeta Telecom",
                          "location": "Nairobi", "url": "https://jobsite.example.com/jobs/care-rep-123",
                          "summary": "Answer customer calls."}],
            }
        )
    )  # fmt: skip
    assert poll(load_eml("generic_alert.eml", user))["jobs"] == 1
    job = Job.objects.get()
    assert (job.origin, job.company, job.url) == (
        "email",
        "Zeta Telecom",
        "https://jobsite.example.com/jobs/care-rep-123",
    )
    assert job.external_id.startswith("email:") and job.description_text == "Answer customer calls."
    assert InboundEmail.objects.get().parser == "generic"
    assert LLMCall.objects.get().user is None


def test_a_newsletter_is_ignored_and_still_filed(user, fake_llm: FakeProvider) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(json.dumps({"is_job_alert": False, "jobs": []}))
    box = mailbox_with(load_eml("newsletter.eml", user))
    services.poll_mailbox()
    record = InboundEmail.objects.get()
    assert (record.status, record.jobs_found) == ("ignored", 0) and not Job.objects.exists()
    assert box.finished == {"1": PROCESSED}


# --- routing, duplicates, idempotency ----------------------------------------------------------------------


def test_an_unrouted_email_is_ignored_and_moved(user, router: respx.MockRouter) -> None:  # type: ignore[no-untyped-def]
    box = mailbox_with(load_eml("unrouted.eml", user))
    counts = services.poll_mailbox()
    assert counts["unrouted"] == 1 and box.finished == {"1": UNROUTED}
    assert (
        not InboundEmail.objects.exists()
        and not Job.objects.exists()
        and not Match.objects.exists()
    )


def test_a_duplicate_message_id_is_processed_once(user) -> None:  # type: ignore[no-untyped-def]
    first, again = (
        load_eml("linkedin_alert.eml", user, uid="1"),
        load_eml("linkedin_alert.eml", user, uid="2"),
    )
    counts = poll(first, again)
    assert counts["parsed"] == 1 and counts["duplicates"] == 1 and counts["jobs"] == 3
    assert (
        InboundEmail.objects.count() == 1
        and Job.objects.count() == 3
        and Match.objects.count() == 3
    )


def test_polling_twice_does_nothing_the_second_time(user) -> None:  # type: ignore[no-untyped-def]
    box = mailbox_with(load_eml("linkedin_alert.eml", user))
    services.poll_mailbox()
    assert services.poll_mailbox()["messages"] == 0  # filed messages are no longer unseen
    assert box.finished == {"1": PROCESSED} and Job.objects.count() == 3


def test_the_same_jobs_in_a_later_email_are_not_created_again(user) -> None:  # type: ignore[no-untyped-def]
    poll(load_eml("linkedin_alert.eml", user))
    raw = (
        "From: jobs@linkedin.com\nTo: "
        + services.ensure_address(user).address
        + "\nSubject: Again\nMessage-ID: <second-1>\n\n"
        "Customer Support Agent\nAcme Ltd\nNairobi, Kenya (Remote)\n\n"
        "View job: https://www.linkedin.com/comm/jobs/view/3912345678?x=1\n"
    )
    mailbox = mailbox_with(parse_message(raw.encode(), uid="9"))
    services.poll_mailbox()
    assert Job.objects.filter(external_id="linkedin:3912345678").count() == 1
    assert Match.objects.for_user(user).count() == 3 and mailbox.finished == {"9": PROCESSED}


def test_a_message_without_a_message_id_is_still_deduplicated(user) -> None:  # type: ignore[no-untyped-def]
    raw = (
        "From: jobs@linkedin.com\nDate: Wed, 04 Mar 2026 06:30:00 +0000\nTo: "
        + services.ensure_address(user).address
        + "\nSubject: No id\n\nSupport Agent\nAcme\nNairobi\n\nView job: https://www.linkedin.com/jobs/view/4999999999\n"
    )
    counts = poll(parse_message(raw.encode(), uid="1"), parse_message(raw.encode(), uid="2"))
    assert counts["parsed"] == 1 and counts["duplicates"] == 1


def test_one_broken_message_does_not_stop_the_others(user, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.inbound.parsers import linkedin

    real = linkedin.LinkedInAlertParser.parse
    calls = {"n": 0}

    def flaky(self: Any, message: Any, who: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(self, message, who)

    monkeypatch.setattr(linkedin.LinkedInAlertParser, "parse", flaky)
    box = mailbox_with(
        load_eml("linkedin_alert.eml", user, uid="1"),
        load_eml("linkedin_alert_text_only.eml", user, uid="2"),
    )
    counts = services.poll_mailbox()
    assert counts["failed"] == 1 and counts["parsed"] == 1 and counts["jobs"] == 2
    failed = InboundEmail.objects.get(status="failed")
    assert failed.error == "processing_error" and "boom" not in failed.error
    assert box.finished == {"1": PROCESSED, "2": PROCESSED}  # not retried forever


def test_nothing_happens_when_the_mailbox_is_not_set_up(user, settings) -> None:  # type: ignore[no-untyped-def]
    settings.INBOUND_MAILBOX = "imap"
    settings.INBOUND_IMAP_USER = settings.INBOUND_IMAP_PASSWORD = ""
    assert services.poll_mailbox() == {
        k: 0 for k in ("messages", "parsed", "jobs", "unrouted", "duplicates", "failed")
    }


# --- Gmail forwarding confirmation -----------------------------------------------------------------------------


def test_a_confirmation_email_becomes_a_banner_for_the_right_user_only(user, client) -> None:  # type: ignore[no-untyped-def]
    bystander = make_user(skills=("Helpdesk",), track=True)
    counts = poll(load_eml("gmail_confirmation.eml", user))
    confirmation = ForwardingConfirmation.objects.get()
    assert counts["jobs"] == 0 and confirmation.user == user
    assert (
        confirmation.confirmation_url
        == "https://mail-settings.google.com/mail/vf-%5BANGjdJ8x%5D-abcdef"
    )
    assert confirmation.confirmation_code == "123456789"
    assert InboundEmail.objects.get().parser == "gmail_confirmation"

    client.force_login(user)
    html = client.get("/app/").content.decode()
    assert "confirm forwarding" in html.lower() and confirmation.confirmation_url in html
    assert "123456789" in html and 'rel="noopener noreferrer"' in html
    client.force_login(bystander)
    assert "confirm forwarding" not in client.get("/app/").content.decode().lower()


def test_only_googles_own_link_is_used(user) -> None:  # type: ignore[no-untyped-def]
    poll(load_eml("gmail_confirmation.eml", user))
    assert "evil.example.net" not in ForwardingConfirmation.objects.get().confirmation_url


def test_a_confirmation_without_googles_link_is_ignored(user) -> None:  # type: ignore[no-untyped-def]
    raw = (
        "From: forwarding-noreply@google.com\nTo: "
        + services.ensure_address(user).address
        + "\nSubject: Gmail Forwarding Confirmation\nMessage-ID: <fc-2>\n\nClick https://evil.example.net/x\n"
    )
    poll(parse_message(raw.encode(), uid="1"))
    assert not ForwardingConfirmation.objects.exists()
    assert InboundEmail.objects.get().status == "ignored"


def test_a_lookalike_sender_is_not_a_confirmation(user) -> None:  # type: ignore[no-untyped-def]
    raw = (
        "From: forwarding-noreply@google.com.evil.example\nTo: "
        + services.ensure_address(user).address
        + "\nSubject: Gmail Forwarding Confirmation\nMessage-ID: <fc-3>\n\nhttps://mail-settings.google.com/mail/vf-x\n"
    )
    poll(parse_message(raw.encode(), uid="1"))
    assert not ForwardingConfirmation.objects.exists()


def test_the_banner_can_be_dismissed_and_only_by_its_owner(user, client) -> None:  # type: ignore[no-untyped-def]
    poll(load_eml("gmail_confirmation.eml", user))
    confirmation = ForwardingConfirmation.objects.get()
    intruder = UserFactory()
    client.force_login(intruder)
    client.post(f"/app/settings/job-alerts/confirmation/{confirmation.pk}/dismiss/")
    assert not ForwardingConfirmation.objects.get().dismissed
    client.force_login(user)
    response = client.post(
        f"/app/settings/job-alerts/confirmation/{confirmation.pk}/dismiss/", {"next": "feed"}
    )
    assert response["Location"] == "/app/" and ForwardingConfirmation.objects.get().dismissed
    assert "confirm forwarding" not in client.get("/app/").content.decode().lower()


def test_a_confirmation_pings_telegram_users_once(tg) -> None:  # type: ignore[no-untyped-def]
    linked = make_notif_user()
    poll(load_eml("gmail_confirmation.eml", linked))
    note = Notification.objects.get(user=linked, kind="system")
    assert notification_services.deliver(note.pk) == "sent"
    assert (
        "confirm forwarding" in tg.messages[0]["text"]
        and "/app/settings/job-alerts/" in tg.messages[0]["text"]
    )


def test_users_without_telegram_get_no_notice(tg) -> None:  # type: ignore[no-untyped-def]
    plain = make_notif_user(chat_id=None)
    poll(load_eml("gmail_confirmation.eml", plain))
    assert not Notification.objects.exists()


# --- addresses --------------------------------------------------------------------------------------------------------


def test_addresses_look_like_the_spec_and_are_unique() -> None:
    users = [UserFactory() for _ in range(30)]
    addresses = [services.ensure_address(u) for u in users]
    assert all(re.fullmatch(r"u-[a-hj-km-np-z2-9]{6}", a.token) for a in addresses)
    assert len({a.token for a in addresses}) == 30
    assert addresses[0].address == f"{addresses[0].token}@alerts.example.test"


def test_ensure_address_is_idempotent() -> None:
    person = UserFactory()
    assert services.ensure_address(person).pk == services.ensure_address(person).pk
    assert InboundAddress.objects.count() == 1


def test_signup_creates_an_address() -> None:
    from apps.accounts.services import complete_signup

    person = UserFactory()
    complete_signup(person, None)
    complete_signup(person, None)  # a replayed signal
    assert InboundAddress.objects.for_user(person).count() == 1


def test_the_data_migration_gives_existing_users_an_address() -> None:
    import importlib

    from django.apps import apps

    migration = importlib.import_module("apps.inbound.migrations.0002_addresses_for_existing_users")
    has_one, without = UserFactory(), [UserFactory(), UserFactory()]
    services.ensure_address(has_one)
    keep = InboundAddress.objects.get(user=has_one).token
    migration.create_addresses(apps, None)
    migration.create_addresses(apps, None)  # running again changes nothing
    assert InboundAddress.objects.count() == 3
    assert InboundAddress.objects.get(user=has_one).token == keep
    assert all(InboundAddress.objects.filter(user=u).exists() for u in without)


# --- retention and logs ------------------------------------------------------------------------------------------------


def test_old_bodies_are_cleared_after_30_days_but_metadata_stays(user) -> None:  # type: ignore[no-untyped-def]
    poll(load_eml("linkedin_alert.eml", user))
    record = InboundEmail.objects.get()
    InboundEmail.objects.filter(pk=record.pk).update(received_at=utc(2026, 1, 1))
    recent = InboundEmail.objects.create(
        user=user, address=record.address, message_id="recent", subject="Recent", body_text="keep",
        received_at=utc(2026, 3, 1),
    )  # fmt: skip
    assert services.clear_old_bodies(now=utc(2026, 3, 4)) == 1
    record.refresh_from_db()
    recent.refresh_from_db()
    assert (
        (record.body_text, record.body_html) == ("", "")
        and record.subject
        and record.jobs_found == 3
    )
    assert recent.body_text == "keep"
    assert services.clear_old_bodies(now=utc(2026, 3, 4)) == 0
    assert services.clear_old_bodies(now=utc(2026, 3, 4) + datetime.timedelta(days=40)) == 1


def test_logs_carry_counts_and_ids_but_no_email_content(user, caplog) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level(logging.DEBUG)
    address = services.ensure_address(user)
    poll(
        load_eml("linkedin_alert.eml", user),
        load_eml("gmail_confirmation.eml", user, uid="2"),
        load_eml("unrouted.eml", user, uid="3"),
    )
    logged = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for secret in (
        address.token, address.address, "Customer Support Agent", "Acme Ltd", "jobalerts-noreply",
        "someone@gmail.com", "3 new jobs", "123456789", "mail-settings.google.com",
    ):  # fmt: skip
        assert secret not in logged, secret
    events = {getattr(r, "event", "") for r in caplog.records}
    assert {"inbound_parsed", "inbound_confirmation", "inbound_unrouted"} <= events


# --- the real IMAP client, against a stand-in library ---------------------------------------------------------------------


def test_the_imap_mailbox_fetches_unseen_mail_and_files_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import imap_tools

    log: list[Any] = []
    raw = b"From: x@linkedin.com\nTo: a@b.example\nSubject: S\nMessage-ID: <imap-1>\n\nbody\n"

    class FakeFolder:
        existing: set[str] = {"INBOX"}

        def exists(self, name: str) -> bool:
            return name in self.existing

        def create(self, name: str) -> None:
            self.existing.add(name)
            log.append(("create", name))

    class FakeBox:
        folder = FakeFolder()

        def fetch(self, criteria: Any, **kwargs: Any) -> list[Any]:
            log.append(("fetch", kwargs))
            return [SimpleNamespace(uid="77", obj=__import__("email").message_from_bytes(raw))]

        def flag(self, uids: list[str], flag: Any, value: bool) -> None:
            log.append(("flag", uids, value))

        def move(self, uids: list[str], folder: str) -> None:
            log.append(("move", uids, folder))

        def logout(self) -> None:
            log.append(("logout",))

    class FakeMailBox:
        def __init__(self, host: str) -> None:
            log.append(("host", host))

        def login(self, user: str, password: str) -> FakeBox:
            log.append(("login", user))
            return FakeBox()

    monkeypatch.setattr(imap_tools, "MailBox", FakeMailBox)
    client = ImapMailbox("imap.example.com", "inbox@example.com", "app-password")
    messages = client.fetch_unseen(10)
    client.finish("77", PROCESSED)
    client.close()
    assert [(m.uid, m.message_id) for m in messages] == [("77", "imap-1")]
    assert ("host", "imap.example.com") in log and ("login", "inbox@example.com") in log
    assert ("create", PROCESSED) in log and ("flag", ["77"], True) in log
    assert ("move", ["77"], PROCESSED) in log and ("logout",) in log
    assert next(e for e in log if e[0] == "fetch")[1]["mark_seen"] is False  # read only when filed


def test_the_real_mailbox_is_used_when_credentials_exist(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.inbound import mailbox

    settings.INBOUND_MAILBOX = "imap"
    settings.INBOUND_IMAP_USER, settings.INBOUND_IMAP_PASSWORD = "u@example.com", "pw"
    made: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        mailbox,
        "ImapMailbox",
        lambda host, user, password: made.append((host, user, password)) or "client",
    )
    assert mailbox.mailbox_enabled() and mailbox.open_mailbox() == "client"
    assert made == [("imap.gmail.com", "u@example.com", "pw")]
