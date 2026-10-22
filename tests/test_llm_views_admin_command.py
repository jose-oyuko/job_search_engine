from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse

from apps.llm.exceptions import LLMProviderError
from apps.llm.models import LLMCall, UsageQuota
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db
User = get_user_model()


def _call(user, **kwargs):  # type: ignore[no-untyped-def]
    return LLMCall.objects.create(
        user=user, task="score_match", provider="fake", model="m", **kwargs
    )


# --- Settings -> Usage -----------------------------------------------------------------------


def test_usage_page_requires_login() -> None:
    response = Client().get(reverse("llm:usage"))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_usage_page_shows_the_users_numbers() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=10, monthly_cost_usd_cap=Decimal("3.00"))
    for _ in range(4):
        _call(user, cost_usd=Decimal("0.25"))
    client = Client()
    client.force_login(user)

    response = client.get("/app/settings/usage/")
    html = response.content.decode()

    assert response.status_code == 200
    assert "4 of 10 used" in html and "Job scoring" in html
    assert "$1.00 of $3.00" in html
    assert "<progress" in html and 'aria-current="page"' in html


def test_usage_page_creates_a_quota_for_users_without_one(settings) -> None:  # type: ignore[no-untyped-def]
    client = Client()
    client.force_login(UserFactory())
    html = client.get("/app/settings/usage/").content.decode()
    assert f"0 of {settings.QUOTA_DEFAULT_MONTHLY_SCORES} used" in html


def test_usage_page_only_shows_the_requesting_users_usage() -> None:
    a, b = UserFactory(), UserFactory()
    UsageQuota.objects.create(user=a, monthly_scores=100)
    UsageQuota.objects.create(user=b, monthly_scores=777)
    for _ in range(3):
        _call(a)
    for _ in range(9):
        _call(b)

    client = Client()
    client.force_login(a)
    html = client.get("/app/settings/usage/").content.decode()
    assert "3 of 100 used" in html
    assert "777" not in html and "9 of" not in html


def test_settings_tabs_link_account_and_usage() -> None:
    client = Client()
    client.force_login(UserFactory())
    html = client.get("/app/settings/account/").content.decode()
    assert "/app/settings/usage/" in html


@pytest.mark.parametrize("name", ["usage"])
def test_llmcall_for_user_is_scoped(name: str) -> None:
    a, b = UserFactory(), UserFactory()
    _call(a)
    _call(b)
    _call(None)
    assert list(LLMCall.objects.for_user(a).values_list("user_id", flat=True)) == [a.pk]


# --- admin spend summary ---------------------------------------------------------------------


@pytest.fixture
def admin_client() -> Client:
    client = Client()
    client.force_login(User.objects.create_superuser("root@example.com", "pw-12345-test"))
    return client


def test_admin_llm_call_list_shows_spend_summary(admin_client: Client) -> None:
    user = UserFactory()
    _call(user, cost_usd=Decimal("0.5"))
    LLMCall.objects.create(
        user=None, task="extract_job", provider="fake", model="m", cost_usd=Decimal("0.25")
    )
    response = admin_client.get(reverse("admin:llm_llmcall_changelist"))
    html = response.content.decode()
    assert response.status_code == 200
    assert "LLM spend" in html and "$0.7500" in html
    assert "extract_job" in html and f"#{user.pk}" in html


def test_admin_llm_calls_are_read_only(admin_client: Client) -> None:
    assert admin_client.get(reverse("admin:llm_llmcall_add")).status_code == 403
    call = _call(UserFactory())
    page = admin_client.post(reverse("admin:llm_llmcall_change", args=[call.pk]), {"model": "x"})
    call.refresh_from_db()
    assert call.model == "m" and page.status_code in (200, 403)


def test_admin_spend_is_staff_only() -> None:
    client = Client()
    client.force_login(UserFactory())
    assert client.get(reverse("admin:llm_llmcall_changelist")).status_code == 302


# --- llm_smoke -------------------------------------------------------------------------------


def test_llm_smoke_runs_against_the_fake_provider() -> None:
    out = StringIO()
    call_command("llm_smoke", stdout=out)
    text = out.getvalue()
    assert "Provider: fake" in text and "ok=True" in text and "pong" in text
    assert "LLM_PROVIDER=fake" in text  # warns it is not a real model
    call = LLMCall.objects.get()
    assert call.task == "smoke" and call.user is None and call.success


def test_llm_smoke_reports_provider_failures(fake_llm) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.append(LLMProviderError("http_401:invalid_api_key"))
    with pytest.raises(CommandError, match="http_401"):
        call_command("llm_smoke", stdout=StringIO())
