"""End-to-end signup through allauth with the invite-only adapters."""

import datetime
import re

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.accounts.models import ConsentRecord, Invite, UserSettings
from apps.accounts.services import complete_signup
from apps.llm.models import UsageQuota
from tests.factories import InviteFactory, UserFactory

pytestmark = pytest.mark.django_db
User = get_user_model()

SIGNUP = {
    "email": "newbie@example.com",
    "password1": "a-Long-pass-phrase-77",
    "password2": "a-Long-pass-phrase-77",
    "full_name": "New Bie",
    "accept_terms": "on",
}


def _join(client: Client, invite: Invite) -> None:
    assert client.get(f"/join/{invite.code}/").status_code == 200


def test_signup_page_without_invite_is_closed() -> None:
    client = Client()
    response = client.get("/accounts/signup/")
    assert "account/signup_closed.html" in [t.name for t in response.templates]


def test_signup_post_without_invite_creates_nothing() -> None:
    client = Client()
    client.post("/accounts/signup/", SIGNUP)
    assert not User.objects.filter(email="newbie@example.com").exists()
    assert mail.outbox == []


def test_join_with_garbage_code_is_404_and_does_not_open_signup() -> None:
    client = Client()
    assert client.get("/join/not-a-real-code/").status_code == 404
    client.post("/accounts/signup/", SIGNUP)
    assert not User.objects.filter(email="newbie@example.com").exists()


@pytest.mark.parametrize(
    "make",
    [
        lambda: InviteFactory(used_by=UserFactory(), used_at=timezone.now()),
        lambda: InviteFactory(expires_at=timezone.now() - datetime.timedelta(minutes=1)),
    ],
    ids=["used", "expired"],
)
def test_used_and_expired_invites_are_refused_at_join(make) -> None:  # type: ignore[no-untyped-def]
    invite = make()
    client = Client()
    response = client.get(f"/join/{invite.code}/")
    assert response.status_code == 404
    client.post("/accounts/signup/", SIGNUP)
    assert not User.objects.filter(email="newbie@example.com").exists()


def test_signup_with_valid_invite_creates_account_and_related_rows() -> None:
    invite = InviteFactory()
    client = Client()
    _join(client, invite)

    response = client.post("/accounts/signup/", SIGNUP)
    assert response.status_code == 302

    user = User.objects.get(email="newbie@example.com")
    assert user.full_name == "New Bie"
    invite.refresh_from_db()
    assert invite.used_by == user and invite.used_at is not None
    assert UserSettings.objects.get(user=user).country == "KE"
    assert UsageQuota.objects.get(user=user).monthly_kits == 40
    kinds = set(ConsentRecord.objects.filter(user=user).values_list("kind", flat=True))
    assert kinds == {"terms", "privacy"}

    # Verification is mandatory: a mail was sent, and the user cannot log in yet.
    (message,) = mail.outbox
    assert message.to == ["newbie@example.com"]
    assert message.alternatives, "verification mail should have an HTML part"
    login = Client().post(
        "/accounts/login/", {"login": "newbie@example.com", "password": SIGNUP["password1"]}
    )
    assert login.status_code == 302 and "confirm-email" in login["Location"]


def test_verification_link_activates_login() -> None:
    invite = InviteFactory()
    client = Client()
    _join(client, invite)
    client.post("/accounts/signup/", SIGNUP)
    link = re.search(r"http\S+/confirm-email/\S+/", mail.outbox[0].body)
    assert link
    key_path = "/" + link.group(0).split("/", 3)[3]
    assert client.post(key_path).status_code == 302
    assert EmailAddress.objects.get(email="newbie@example.com").verified

    fresh = Client()
    response = fresh.post(
        "/accounts/login/", {"login": "newbie@example.com", "password": SIGNUP["password1"]}
    )
    assert response.status_code == 302
    assert fresh.get("/app/").status_code == 200


def test_invite_cannot_be_used_twice() -> None:
    invite = InviteFactory()
    first = Client()
    _join(first, invite)
    first.post("/accounts/signup/", SIGNUP)

    second = Client()
    assert second.get(f"/join/{invite.code}/").status_code == 404
    second.post("/accounts/signup/", {**SIGNUP, "email": "other@example.com"})
    assert not User.objects.filter(email="other@example.com").exists()


def test_consent_checkbox_is_required() -> None:
    invite = InviteFactory()
    client = Client()
    _join(client, invite)
    data = {k: v for k, v in SIGNUP.items() if k != "accept_terms"}
    response = client.post("/accounts/signup/", data)
    assert response.status_code == 200
    assert b"accept the terms" in response.content
    assert not User.objects.filter(email="newbie@example.com").exists()


def test_email_locked_invite_rejects_other_emails() -> None:
    invite = InviteFactory(email="friend@example.com")
    client = Client()
    _join(client, invite)

    wrong = client.post("/accounts/signup/", {**SIGNUP, "email": "stranger@example.com"})
    assert wrong.status_code == 200
    assert b"different email address" in wrong.content
    assert not User.objects.filter(email="stranger@example.com").exists()

    right = client.post("/accounts/signup/", {**SIGNUP, "email": "Friend@Example.com"})
    assert right.status_code == 302
    assert User.objects.filter(email="friend@example.com").exists()


def test_join_while_logged_in_does_not_open_a_second_signup() -> None:
    invite = InviteFactory()
    client = Client()
    client.force_login(UserFactory())
    response = client.get(f"/join/{invite.code}/")
    assert response.status_code == 302 and response["Location"] == "/app/"


def test_complete_signup_is_idempotent() -> None:
    user = UserFactory()
    invite = InviteFactory()
    complete_signup(user, invite.code)
    complete_signup(user, invite.code)
    assert UserSettings.objects.filter(user=user).count() == 1
    assert UsageQuota.objects.filter(user=user).count() == 1
    assert ConsentRecord.objects.filter(user=user).count() == 2


def test_losing_an_invite_race_does_not_overwrite_the_winner() -> None:
    winner, loser = UserFactory(), UserFactory()
    invite = InviteFactory()
    complete_signup(winner, invite.code)
    complete_signup(loser, invite.code)
    invite.refresh_from_db()
    assert invite.used_by == winner


def test_quota_defaults_come_from_settings(settings) -> None:  # type: ignore[no-untyped-def]
    settings.QUOTA_DEFAULT_MONTHLY_KITS = 7
    user = UserFactory()
    complete_signup(user, None)
    assert UsageQuota.objects.get(user=user).monthly_kits == 7
