"""Settings → Job alerts, the onboarding step and isolation for inbound emails (spec §9, §16)."""

import pytest
from django.test import Client

from apps.inbound import services
from apps.inbound.models import ForwardingConfirmation, InboundAddress, InboundEmail
from tests.factories import UserFactory
from tests.inbound_helpers import load_eml, mailbox_with
from tests.matching_helpers import make_user
from tests.notif_helpers import utc

pytestmark = pytest.mark.django_db
URL = "/app/settings/job-alerts/"


@pytest.fixture
def me():  # type: ignore[no-untyped-def]
    user = make_user(skills=("Helpdesk",), track=True)
    client = Client()
    client.force_login(user)
    return client, user


def test_the_page_shows_the_address_a_copy_button_and_both_sets_of_instructions(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    html = client.get(URL).content.decode()
    address = services.ensure_address(user).address
    assert address in html and 'data-copy="#inbound-address"' in html
    assert "Gmail: forward your job alerts" in html and "Outlook: forward your job alerts" in html
    assert "Nothing has arrived yet" in html and "kit.js" in html


def test_the_page_creates_the_address_on_first_visit(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    assert not InboundAddress.objects.exists()
    client.get(URL)
    assert InboundAddress.objects.for_user(user).count() == 1
    client.get(URL)
    assert InboundAddress.objects.count() == 1


def test_recent_emails_are_listed_with_their_result(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    mailbox_with(load_eml("linkedin_alert.eml", user), load_eml("unrouted.eml", user, uid="2"))
    services.poll_mailbox()
    html = client.get(URL).content.decode()
    assert "3 new jobs for Customer Support Agent in Kenya" in html
    assert "Read" in html and ">3<" in html
    assert "1 new job" not in html  # the unrouted email was never recorded


def test_the_page_warns_when_ingestion_is_off(me, settings) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    settings.INBOUND_MAILBOX = "imap"
    settings.INBOUND_IMAP_USER = settings.INBOUND_IMAP_PASSWORD = ""
    assert "not switched on for this server" in client.get(URL).content.decode()


def test_the_page_needs_login_and_only_accepts_get() -> None:
    assert "/accounts/login/" in Client().get(URL)["Location"]
    user = UserFactory()
    client = Client()
    client.force_login(user)
    assert client.post(URL).status_code == 405


def test_the_settings_nav_links_to_job_alerts(me) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    assert URL in client.get("/app/settings/account/").content.decode()


def test_the_confirmation_banner_shows_on_the_settings_page_too(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    mailbox_with(load_eml("gmail_confirmation.eml", user))
    services.poll_mailbox()
    html = client.get(URL).content.decode()
    assert "Confirm forwarding" in html and "mail-settings.google.com" in html


def test_the_onboarding_step_shows_the_address_and_continues(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    html = client.get("/welcome/alerts/").content.decode()
    assert services.ensure_address(user).address in html and "Step 6 of 6" in html
    assert "Optional" in html and 'href="/welcome/done/"' in html


# --- isolation ------------------------------------------------------------------------------------------------


def test_one_users_emails_never_appear_for_another(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    other = make_user(skills=("Helpdesk",), track=True)
    mailbox_with(load_eml("linkedin_alert.eml", other))
    services.poll_mailbox()
    html = client.get(URL).content.decode()
    assert "3 new jobs" not in html and "Nothing has arrived yet" in html
    assert InboundEmail.objects.for_user(user).count() == 0
    assert InboundEmail.objects.for_user(other).count() == 1


def test_an_email_to_one_address_never_reaches_another_user(me) -> None:  # type: ignore[no-untyped-def]
    _, user = me
    other = make_user(skills=("Helpdesk",), track=True)
    services.ensure_address(user)
    mailbox_with(load_eml("linkedin_alert.eml", other))
    services.poll_mailbox()
    from apps.matching.models import Match

    assert Match.objects.for_user(other).count() == 3 and Match.objects.for_user(user).count() == 0
    assert ForwardingConfirmation.objects.count() == 0


def test_for_user_scopes_every_inbound_model(me) -> None:  # type: ignore[no-untyped-def]
    _, user = me
    other = make_user(skills=("Helpdesk",), track=True)
    mailbox_with(
        load_eml("gmail_confirmation.eml", other), load_eml("linkedin_alert.eml", other, uid="2")
    )
    services.poll_mailbox()
    services.ensure_address(user)
    assert InboundAddress.objects.for_user(user).count() == 1
    assert (
        InboundEmail.objects.for_user(user).count() == 0
        and InboundEmail.objects.for_user(other).count() == 2
    )
    assert ForwardingConfirmation.objects.for_user(user).count() == 0
    assert ForwardingConfirmation.objects.for_user(other).count() == 1
    for model in (InboundAddress, InboundEmail, ForwardingConfirmation):
        assert model.objects.for_user(None).count() == 0


def test_one_user_cannot_dismiss_anothers_confirmation(me) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    other = make_user(skills=("Helpdesk",), track=True)
    mailbox_with(load_eml("gmail_confirmation.eml", other))
    services.poll_mailbox()
    confirmation = ForwardingConfirmation.objects.get()
    client.post(f"/app/settings/job-alerts/confirmation/{confirmation.pk}/dismiss/")
    confirmation.refresh_from_db()
    assert confirmation.dismissed is False


def test_the_admin_shows_no_sender_subject_or_body(me, admin_client) -> None:  # type: ignore[no-untyped-def]
    _, user = me
    mailbox_with(load_eml("linkedin_alert.eml", user))
    services.poll_mailbox()
    record = InboundEmail.objects.get()
    page = admin_client.get(f"/admin/inbound/inboundemail/{record.pk}/change/").content.decode()
    assert "jobalerts-noreply" not in page and "Customer Support Agent" not in page
    assert "3 new jobs" not in page and "body_text" not in page
    assert utc(2026, 3, 4) < record.received_at
