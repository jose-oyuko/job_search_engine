"""Startup validation of required production variables (kept separate so it is testable)."""

from collections.abc import Mapping

REQUIRED_ENV = [
    "SECRET_KEY",
    "DATABASE_URL",
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "SITE_URL",
    "EMAIL_HOST",
    "DEFAULT_FROM_EMAIL",
]
REQUIRED_WHEN_LLM_REAL = ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL_FAST", "LLM_MODEL_WRITER"]
PLACEHOLDER_SECRETS = {"change-me", "insecure-dev-only-key"}


def missing_required_env(environ: Mapping[str, str]) -> list[str]:
    required = list(REQUIRED_ENV)
    if environ.get("LLM_PROVIDER", "fake") != "fake":
        required += REQUIRED_WHEN_LLM_REAL
    missing = [name for name in required if not environ.get(name)]
    if environ.get("SECRET_KEY") in PLACEHOLDER_SECRETS:
        missing.append("SECRET_KEY (placeholder value is not allowed)")
    return missing
