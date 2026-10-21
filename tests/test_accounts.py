import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db


def test_custom_user_model_is_in_use() -> None:
    assert settings.AUTH_USER_MODEL == "accounts.User"
    assert get_user_model()._meta.label == "accounts.User"


def test_email_is_the_login_field_and_is_normalised() -> None:
    User = get_user_model()
    user = User.objects.create_user("  Jane@Example.COM ", "pw-12345-test")
    assert User.USERNAME_FIELD == "email"
    assert user.email == "jane@example.com"
    assert user.check_password("pw-12345-test")


def test_create_user_requires_email() -> None:
    with pytest.raises(ValueError):
        get_user_model().objects.create_user("", "pw")


def test_create_superuser_flags() -> None:
    admin = get_user_model().objects.create_superuser("root@example.com", "pw-12345-test")
    assert admin.is_staff and admin.is_superuser
