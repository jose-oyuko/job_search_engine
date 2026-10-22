import datetime

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.exceptions import (
    InviteAlreadyUsed,
    InviteEmailMismatch,
    InviteExpired,
    InviteNotFound,
)
from apps.accounts.models import Invite
from apps.accounts.services import (
    generate_invites,
    invite_url,
    send_invite_email,
    validate_invite,
)
from tests.factories import InviteFactory, UserFactory

pytestmark = pytest.mark.django_db


def test_valid_invite_passes() -> None:
    invite = InviteFactory()
    assert validate_invite(invite.code) == invite


@pytest.mark.parametrize("code", ["", "does-not-exist"])
def test_unknown_or_empty_code_is_rejected(code: str) -> None:
    with pytest.raises(InviteNotFound):
        validate_invite(code)


def test_used_invite_is_rejected() -> None:
    invite = InviteFactory(used_by=UserFactory(), used_at=timezone.now())
    with pytest.raises(InviteAlreadyUsed):
        validate_invite(invite.code)


def test_expired_invite_is_rejected() -> None:
    invite = InviteFactory(expires_at=timezone.now() - datetime.timedelta(seconds=1))
    with pytest.raises(InviteExpired):
        validate_invite(invite.code)


def test_email_locked_invite_rejects_other_addresses() -> None:
    invite = InviteFactory(email="friend@example.com")
    with pytest.raises(InviteEmailMismatch):
        validate_invite(invite.code, email="someone-else@example.com")


def test_email_lock_is_case_insensitive_and_unlocked_invites_accept_any_email() -> None:
    locked = InviteFactory(email="friend@example.com")
    assert validate_invite(locked.code, email="  Friend@Example.COM ") == locked
    open_invite = InviteFactory()
    assert validate_invite(open_invite.code, email="anyone@example.com") == open_invite


def test_codes_are_unique_and_url_safe() -> None:
    codes = {i.code for i in generate_invites(created_by=None, count=20)}
    assert len(codes) == 20
    assert all(c.replace("-", "").replace("_", "").isalnum() for c in codes)


def test_generate_invites_sets_expiry_and_creator() -> None:
    admin = UserFactory(is_staff=True)
    (invite,) = generate_invites(created_by=admin, expires_in_days=3, note="friends")
    assert invite.created_by == admin
    assert invite.note == "friends"
    remaining = invite.expires_at - timezone.now()
    assert datetime.timedelta(days=2, hours=23) < remaining <= datetime.timedelta(days=3)


def test_email_lock_requires_a_single_invite() -> None:
    with pytest.raises(ValueError):
        generate_invites(created_by=None, count=2, email="a@example.com")


def test_invite_str_never_contains_the_code() -> None:
    invite = InviteFactory()
    assert invite.code not in str(invite)


def test_invite_url_points_at_the_join_page() -> None:
    invite = Invite.objects.create()
    assert invite_url(invite).endswith(f"/join/{invite.code}/")


def test_send_invite_email_renders_html_and_text() -> None:
    invite = InviteFactory(email="friend@example.com")
    assert send_invite_email(invite, "friend@example.com") is True
    (message,) = mail.outbox
    assert message.to == ["friend@example.com"]
    assert invite_url(invite) in message.body
    assert message.alternatives
    assert invite_url(invite) in message.alternatives[0].content  # type: ignore[union-attr]


def test_send_invite_email_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError

    monkeypatch.setattr("django.core.mail.message.EmailMessage.send", boom)
    assert send_invite_email(InviteFactory(), "friend@example.com") is False
