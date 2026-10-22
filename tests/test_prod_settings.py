import os
import subprocess
import sys
from pathlib import Path

from jobpilot.settings.env_check import (
    REQUIRED_ENV,
    REQUIRED_WHEN_LLM_REAL,
    missing_required_env,
)

BASE_DIR = Path(__file__).resolve().parent.parent

COMPLETE_ENV = {
    "SECRET_KEY": "a-real-secret-for-the-test-run",
    "DATABASE_URL": "sqlite:///:memory:",
    "ALLOWED_HOSTS": "example.com",
    "CSRF_TRUSTED_ORIGINS": "https://example.com",
    "SITE_URL": "https://example.com",
    "EMAIL_HOST": "smtp.example.com",
    "DEFAULT_FROM_EMAIL": "JobPilot <no-reply@example.com>",
}


def _load_prod_settings(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Drop everything prod validates, so a developer's .env or shell cannot change the outcome.
    scrubbed = {*REQUIRED_ENV, *REQUIRED_WHEN_LLM_REAL, "LLM_PROVIDER"}
    env = {k: v for k, v in os.environ.items() if k not in scrubbed}
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "jobpilot.settings.prod",
            "ENV_FILE": str(BASE_DIR / "does-not-exist.env"),
            "HUEY_DB_PATH": str(BASE_DIR / "var" / "test-prod-huey.db"),
            **env_overrides,
        }
    )
    return subprocess.run(  # noqa: S603 - fixed interpreter, no user input
        [sys.executable, "-c", "from django.conf import settings; print(settings.SECRET_KEY)"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_prod_refuses_to_start_and_lists_missing_variables() -> None:
    result = _load_prod_settings({})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    for name in ("SECRET_KEY", "DATABASE_URL", "ALLOWED_HOSTS", "EMAIL_HOST"):
        assert name in result.stderr


def test_prod_rejects_placeholder_secret_key() -> None:
    result = _load_prod_settings({**COMPLETE_ENV, "SECRET_KEY": "change-me"})
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_prod_starts_with_complete_environment() -> None:
    result = _load_prod_settings(COMPLETE_ENV)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == COMPLETE_ENV["SECRET_KEY"]


def test_prod_requires_llm_variables_only_for_real_provider() -> None:
    result = _load_prod_settings({**COMPLETE_ENV, "LLM_PROVIDER": "openai_compat"})
    assert result.returncode != 0
    for name in ("LLM_API_KEY", "LLM_MODEL_FAST", "LLM_MODEL_WRITER", "LLM_BASE_URL"):
        assert name in result.stderr


def test_missing_required_env_pure_function() -> None:
    assert missing_required_env(COMPLETE_ENV) == []
    assert "SECRET_KEY" in missing_required_env({})
