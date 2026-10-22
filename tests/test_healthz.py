import pytest
from django.test import Client
from django.urls import reverse

from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_healthz_ok() -> None:
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_not_cached_by_route_name() -> None:
    assert reverse("core:healthz") == "/healthz"


def test_deep_check_requires_staff() -> None:
    assert Client().get("/healthz?deep=1").status_code == 403
    client = Client()
    client.force_login(UserFactory())
    assert client.get("/healthz?deep=1").status_code == 403


def test_deep_check_for_staff() -> None:
    client = Client()
    client.force_login(UserFactory(is_staff=True))
    response = client.get("/healthz?deep=1")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "huey_pending" in response.json()


def test_home_page_renders() -> None:
    response = Client().get("/")
    assert response.status_code == 200
    assert b"JobPilot" in response.content
