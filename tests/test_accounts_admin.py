import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Invite, WaitlistEntry
from apps.accounts.services import invite_url
from tests.factories import InviteFactory, UserFactory, WaitlistEntryFactory

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def admin_client() -> Client:
    client = Client()
    client.force_login(User.objects.create_superuser("root@example.com", "pw-12345-test"))
    return client


def test_admin_approve_action_creates_invite_and_sends_email(admin_client: Client) -> None:
    entry = WaitlistEntryFactory(email="friend@example.com")
    response = admin_client.post(
        reverse("admin:accounts_waitlistentry_changelist"),
        {"action": "approve", "_selected_action": [entry.pk]},
        follow=True,
    )
    assert response.status_code == 200
    entry.refresh_from_db()
    assert entry.status == "invited" and entry.invite is not None
    assert entry.invite.email == "friend@example.com"
    (message,) = mail.outbox
    assert message.to == ["friend@example.com"]
    assert invite_url(entry.invite) in message.body


def test_admin_reject_action(admin_client: Client) -> None:
    entry = WaitlistEntryFactory()
    admin_client.post(
        reverse("admin:accounts_waitlistentry_changelist"),
        {"action": "reject", "_selected_action": [entry.pk]},
    )
    entry.refresh_from_db()
    assert entry.status == WaitlistEntry.Status.REJECTED
    assert mail.outbox == []


def test_admin_bulk_generate_invites(admin_client: Client) -> None:
    url = reverse("admin:accounts_invite_generate")
    assert admin_client.get(url).status_code == 200
    response = admin_client.post(url, {"count": 3, "note": "friends", "expires_in_days": 7})
    assert response.status_code == 200
    assert Invite.objects.count() == 3
    for invite in Invite.objects.all():
        assert invite_url(invite) in response.content.decode()
        assert invite.note == "friends" and invite.created_by.email == "root@example.com"


def test_admin_copy_links_and_email_actions(admin_client: Client) -> None:
    locked = InviteFactory(email="friend@example.com")
    response = admin_client.post(
        reverse("admin:accounts_invite_changelist"),
        {"action": "copy_links", "_selected_action": [locked.pk]},
        follow=True,
    )
    assert invite_url(locked) in response.content.decode()

    admin_client.post(
        reverse("admin:accounts_invite_changelist"),
        {"action": "email_invites", "_selected_action": [locked.pk]},
    )
    assert [m.to for m in mail.outbox] == [["friend@example.com"]]


def test_admin_add_invite_sets_creator_and_shows_link(admin_client: Client) -> None:
    response = admin_client.post(
        reverse("admin:accounts_invite_add"),
        {
            "email": "Lock@Example.com",
            "note": "n",
            "expires_at_0": "2030-01-01",
            "expires_at_1": "10:00:00",
        },
    )
    assert response.status_code == 302
    invite = Invite.objects.get()
    assert invite.email == "lock@example.com" and invite.created_by.email == "root@example.com"
    detail = admin_client.get(reverse("admin:accounts_invite_change", args=[invite.pk]))
    assert invite.code in detail.content.decode()


def test_admin_pages_are_staff_only() -> None:
    client = Client()
    client.force_login(UserFactory())
    for name in ("admin:accounts_invite_changelist", "admin:accounts_invite_generate"):
        assert client.get(reverse(name)).status_code == 302  # bounced to the admin login


def test_user_admin_shows_settings_and_quota_inlines(admin_client: Client) -> None:
    from apps.accounts.services import complete_signup

    user = UserFactory()
    complete_signup(user, None)
    page = admin_client.get(reverse("admin:accounts_user_change", args=[user.pk]))
    assert page.status_code == 200
    assert b"monthly_kits" in page.content and b"notify_mode" in page.content


def test_admin_created_user_gets_settings_and_quota(admin_client: Client) -> None:
    admin_client.post(
        reverse("admin:accounts_user_add"),
        {
            "email": "made@example.com",
            "password1": "a-Long-pass-phrase-77",
            "password2": "a-Long-pass-phrase-77",
        },
    )
    user = User.objects.get(email="made@example.com")
    assert user.settings.country == "KE" and user.usage_quota.monthly_scores == 600
