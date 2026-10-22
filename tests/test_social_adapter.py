"""Google sign-in is gated by the same invite rule; tested at the adapter level (no network)."""

import pytest
from allauth.account.models import EmailAddress
from allauth.core.context import request_context
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory

from apps.accounts.adapters import InviteOnlySocialAccountAdapter
from apps.accounts.services import INVITE_SESSION_KEY
from tests.factories import InviteFactory, UserFactory

pytestmark = pytest.mark.django_db
User = get_user_model()


def _request(code: str | None):  # type: ignore[no-untyped-def]
    request = RequestFactory().get("/")
    request.session = SessionStore()
    if code:
        request.session[INVITE_SESSION_KEY] = code
    return request


def _sociallogin(email: str = "g@example.com") -> SocialLogin:
    user = User(email=email)
    return SocialLogin(
        user=user,
        account=SocialAccount(provider="google", uid="123", extra_data={"name": "Gee Oogle"}),
        email_addresses=[EmailAddress(email=email, verified=True, primary=True)],
    )


def test_google_signup_refused_without_invite() -> None:
    assert (
        InviteOnlySocialAccountAdapter().is_open_for_signup(_request(None), _sociallogin()) is False
    )


def test_google_signup_refused_with_unknown_code() -> None:
    adapter = InviteOnlySocialAccountAdapter()
    assert adapter.is_open_for_signup(_request("bogus"), _sociallogin()) is False


def test_google_signup_allowed_with_valid_invite() -> None:
    invite = InviteFactory()
    adapter = InviteOnlySocialAccountAdapter()
    assert adapter.is_open_for_signup(_request(invite.code), _sociallogin()) is True


def test_google_signup_respects_the_invite_email_lock() -> None:
    invite = InviteFactory(email="friend@example.com")
    adapter = InviteOnlySocialAccountAdapter()
    assert (
        adapter.is_open_for_signup(_request(invite.code), _sociallogin("other@example.com"))
        is False
    )
    assert (
        adapter.is_open_for_signup(_request(invite.code), _sociallogin("Friend@example.com"))
        is True
    )


def test_google_signup_refused_for_a_used_invite() -> None:
    from django.utils import timezone

    invite = InviteFactory(used_by=UserFactory(), used_at=timezone.now())
    adapter = InviteOnlySocialAccountAdapter()
    assert adapter.is_open_for_signup(_request(invite.code), _sociallogin()) is False


def test_google_name_is_copied_to_the_user() -> None:
    adapter = InviteOnlySocialAccountAdapter()
    login = _sociallogin()
    user = adapter.populate_user(_request(None), login, {"email": "g@example.com"})
    assert user.full_name == "Gee Oogle"


def _bare_request(code: str | None):  # type: ignore[no-untyped-def]
    from django.contrib.messages.storage.fallback import FallbackStorage

    request = _request(code)
    request.user = AnonymousUser()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    return request


def test_new_google_user_without_invite_gets_the_signup_closed_page() -> None:
    """Through allauth's own signup flow, not just the adapter method."""
    from allauth.socialaccount.helpers import complete_social_login
    from allauth.socialaccount.models import SocialApp

    # Google is unconfigured in tests (no env), so register a DB-backed app for the provider.
    app = SocialApp.objects.create(provider="google", name="google", client_id="x", secret="y")
    request = _bare_request(None)
    with request_context(request):
        login = _sociallogin()
        login.provider = app.get_provider(request)
        login.state = {"process": "login"}
        response = complete_social_login(request, login)
    assert response.status_code == 200
    assert b"An invite is required" in response.content
    assert not User.objects.filter(email="g@example.com").exists()


def test_existing_user_can_still_log_in_with_google_without_an_invite() -> None:
    """is_open_for_signup is only asked for *new* accounts; a known Google account logs in."""
    from allauth.socialaccount.helpers import complete_social_login

    user = UserFactory(email="g@example.com")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    SocialAccount.objects.create(user=user, provider="google", uid="123")

    login = _sociallogin()  # unsaved user + same provider/uid: allauth must find the account
    login.state = {"process": "login"}
    request = _bare_request(None)
    with request_context(request):
        response = complete_social_login(request, login)
    assert response.status_code == 302
    assert request.user.is_authenticated or request.session.get("_auth_user_id") == str(user.pk)
