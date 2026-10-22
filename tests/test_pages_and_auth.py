"""Public pages, error pages, login/logout/password reset, and per-user isolation."""

import re

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.accounts.models import ConsentRecord, UserSettings
from apps.accounts.services import complete_signup
from apps.llm.models import UsageQuota
from apps.pages.views import permission_denied, server_error
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db
User = get_user_model()
PASSWORD = "pass-12345-test"


def _verified_user(email: str = "member@example.com"):  # type: ignore[no-untyped-def]
    user = UserFactory(email=email)
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    complete_signup(user, None)
    UserSettings.objects.filter(user=user).update(onboarding_completed=True)
    return user


@pytest.mark.parametrize("name", ["pages:landing", "pages:privacy", "pages:terms"])
def test_public_pages_render(name: str) -> None:
    response = Client().get(reverse(name))
    assert response.status_code == 200
    assert b"viewport" in response.content


def test_landing_has_waitlist_form_and_legal_links() -> None:
    html = Client().get("/").content.decode()
    assert 'action="/waitlist/"' in html
    assert "/privacy/" in html and "/terms/" in html


def test_privacy_policy_covers_required_topics() -> None:
    text = Client().get("/privacy/").content.decode().lower()
    for topic in (
        "language-model provider",
        "forwarded job-alert",
        "30 days",
        "12 months",
        "delete",
        "contact",
    ):
        assert topic in text


def test_unknown_url_renders_the_custom_404() -> None:
    response = Client().get("/definitely/not/here/")
    assert response.status_code == 404
    assert b"We could not find that page" in response.content


def test_500_page_renders_without_a_request() -> None:
    response = server_error(RequestFactory().get("/"))
    assert response.status_code == 500
    assert b"Something went wrong" in response.content


@pytest.mark.urls("tests.urls_boom")
def test_unhandled_exception_serves_the_500_page() -> None:
    response = Client(raise_request_exception=False).get("/boom/")
    assert response.status_code == 500
    assert b"Something went wrong" in response.content


def test_403_page_renders() -> None:
    from django.core.exceptions import PermissionDenied

    response = permission_denied(RequestFactory().get("/"), PermissionDenied())
    assert response.status_code == 403


def test_login_logout_flow() -> None:
    _verified_user()
    client = Client()
    response = client.post(
        "/accounts/login/", {"login": "member@example.com", "password": PASSWORD}
    )
    assert response.status_code == 302 and response["Location"] == "/app/"
    assert client.get("/app/").status_code == 200

    client.post("/accounts/logout/")
    assert client.get("/app/").status_code == 302  # back to login


def test_login_with_wrong_password_fails() -> None:
    _verified_user()
    response = Client().post(
        "/accounts/login/", {"login": "member@example.com", "password": "nope"}
    )
    assert response.status_code == 200
    assert b"not correct" in response.content


def test_login_is_rate_limited_to_ten_per_minute_per_ip() -> None:
    client = Client()
    codes = [
        client.post("/accounts/login/", {"login": "x@example.com", "password": "bad"}).status_code
        for _ in range(11)
    ]
    assert codes[-1] == 429
    assert 429 not in codes[:10]


def test_password_reset_sends_branded_email_and_resets() -> None:
    user = _verified_user()
    client = Client()
    assert client.post("/accounts/password/reset/", {"email": user.email}).status_code == 302
    (message,) = mail.outbox
    assert message.alternatives, "reset mail should have an HTML part"
    assert "JobPilot" in message.alternatives[0].content  # type: ignore[union-attr]

    link = re.search(r"http\S+/password/reset/key/\S+/", message.body)
    assert link
    path = "/" + link.group(0).split("/", 3)[3]
    redirect = client.get(path)  # allauth swaps the key into the session, then redirects
    new_password = "brand-New-pass-99"
    done = client.post(redirect["Location"], {"password1": new_password, "password2": new_password})
    assert done.status_code == 302
    assert (
        Client()
        .post("/accounts/login/", {"login": user.email, "password": new_password})
        .status_code
        == 302
    )


def test_dashboard_requires_login() -> None:
    response = Client().get("/app/")
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_logged_in_user_sees_app_shell_with_nav() -> None:
    client = Client()
    client.force_login(_verified_user())
    html = client.get("/app/").content.decode()
    assert "Settings" in html and "Jobs" in html
    assert "md:hidden" in html  # mobile bottom nav present alongside the desktop sidebar


def test_landing_redirects_signed_in_users_to_the_app() -> None:
    client = Client()
    client.force_login(_verified_user())
    assert client.get("/")["Location"] == "/app/"


def test_account_settings_page() -> None:
    client = Client()
    assert client.get("/app/settings/account/").status_code == 302
    client.force_login(_verified_user())
    response = client.get("/app/settings/account/")
    assert response.status_code == 200 and b"member@example.com" in response.content


# --- multi-user isolation (spec §16) -----------------------------------------------------------


@pytest.mark.parametrize("model", [UserSettings, UsageQuota, ConsentRecord])
def test_for_user_only_returns_the_requesting_users_rows(model) -> None:  # type: ignore[no-untyped-def]
    a, b = _verified_user("a@example.com"), _verified_user("b@example.com")
    mine = model.objects.for_user(a)
    assert mine.exists()
    assert not mine.exclude(user=a).exists()
    assert not model.objects.for_user(b).filter(user=a).exists()


@pytest.mark.parametrize("model", [UserSettings, UsageQuota, ConsentRecord])
def test_for_user_is_empty_for_anonymous(model) -> None:  # type: ignore[no-untyped-def]
    from django.contrib.auth.models import AnonymousUser

    _verified_user()
    assert model.objects.for_user(AnonymousUser()).count() == 0


def test_account_page_shows_only_the_requesting_users_email() -> None:
    a, b = _verified_user("a@example.com"), _verified_user("b@example.com")
    client = Client()
    client.force_login(a)
    content = client.get("/app/settings/account/").content.decode()
    assert a.email in content and b.email not in content
