import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Invite, WaitlistEntry
from apps.accounts.services import approve_waitlist_entry, submit_waitlist
from tests.factories import UserFactory, WaitlistEntryFactory
from tests.logcapture import ListHandler

pytestmark = pytest.mark.django_db

FORM = {"name": "Ada Lovelace", "email": "ada@example.com", "reason": "Remote data work"}


def test_waitlist_form_saves_an_entry() -> None:
    response = Client().post(reverse("pages:waitlist"), FORM)
    assert response.status_code == 200
    assert b"we have your request" in response.content
    entry = WaitlistEntry.objects.get()
    assert (entry.name, entry.email, entry.status) == ("Ada Lovelace", "ada@example.com", "pending")


def test_waitlist_normalises_email_and_does_not_duplicate() -> None:
    client = Client()
    client.post(reverse("pages:waitlist"), FORM)
    again = client.post(reverse("pages:waitlist"), {**FORM, "email": "ADA@example.com"})
    assert again.status_code == 200  # same thank-you: the form does not reveal known addresses
    assert WaitlistEntry.objects.count() == 1


def test_waitlist_rejects_invalid_input() -> None:
    response = Client().post(reverse("pages:waitlist"), {"name": "", "email": "nope"})
    assert response.status_code == 400
    assert WaitlistEntry.objects.count() == 0


def test_waitlist_honeypot_pretends_success_and_stores_nothing() -> None:
    response = Client().post(reverse("pages:waitlist"), {**FORM, "website": "http://spam"})
    assert response.status_code == 200
    assert WaitlistEntry.objects.count() == 0


def test_waitlist_is_rate_limited_to_five_per_hour_per_ip() -> None:
    client = Client()
    codes = [
        client.post(reverse("pages:waitlist"), {**FORM, "email": f"p{i}@example.com"}).status_code
        for i in range(6)
    ]
    assert codes == [200] * 5 + [429]
    assert WaitlistEntry.objects.count() == 5


def test_waitlist_limit_is_per_ip() -> None:
    for i in range(5):
        Client(REMOTE_ADDR="10.0.0.1").post(
            reverse("pages:waitlist"), {**FORM, "email": f"a{i}@example.com"}
        )
    other = Client(REMOTE_ADDR="10.0.0.2").post(reverse("pages:waitlist"), FORM)
    assert other.status_code == 200


def test_waitlist_logs_the_record_id_but_not_the_email(handler: ListHandler) -> None:
    submit_waitlist(name="Ada", email="ada@example.com", reason="x")
    (line,) = handler.events("waitlist_entry_created")
    assert "waitlist_entry_id" in line
    assert "ada@example.com" not in "".join(handler.lines)


def test_only_get_is_not_allowed_for_waitlist_endpoint() -> None:
    assert Client().get(reverse("pages:waitlist")).status_code == 405


def test_approve_creates_a_locked_invite_and_emails_it() -> None:
    entry = WaitlistEntryFactory(email="friend@example.com")
    admin = UserFactory(is_staff=True)
    invite, emailed = approve_waitlist_entry(entry, admin)

    entry.refresh_from_db()
    assert emailed is True
    assert entry.status == WaitlistEntry.Status.INVITED and entry.invite == invite
    assert invite.email == "friend@example.com" and invite.created_by == admin
    (message,) = mail.outbox
    assert message.to == ["friend@example.com"]
    assert invite.code in message.body


def test_approving_twice_does_not_send_a_second_email() -> None:
    entry = WaitlistEntryFactory()
    first, _ = approve_waitlist_entry(entry, None)
    second, emailed = approve_waitlist_entry(entry, None)
    assert second == first and emailed is False
    assert len(mail.outbox) == 1
    assert Invite.objects.count() == 1
