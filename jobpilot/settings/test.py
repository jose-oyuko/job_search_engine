"""Test settings: SQLite in memory, fake LLM, eager Huey, locmem email, no network."""

import tempfile
from pathlib import Path

from .base import *  # noqa: F403
from .base import HUEY, INSTALLED_APPS

DEBUG = False
SECRET_KEY = "test-only-secret-key-not-used-anywhere-else"  # noqa: S105

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

# Test-only models live in tests/testapp (used to exercise abstract base classes).
INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]

HUEY = {**HUEY, "immediate": True}

LLM_PROVIDER = "fake"
TELEGRAM_GATEWAY = "fake"
TELEGRAM_BOT_USERNAME = "jobpilot_test_bot"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
PRIVATE_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="jobpilot-private-"))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Tests never sleep between requests.
SCRAPER_DELAY_SECONDS = 0.0
