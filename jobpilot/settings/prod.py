"""Production settings. Fails fast, listing every missing required variable (§21.2)."""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env
from .env_check import missing_required_env

_missing = missing_required_env(os.environ)
if _missing:
    raise ImproperlyConfigured(
        "Missing required environment variables for production: " + ", ".join(_missing)
    )

DEBUG = False
SECRET_KEY = env.str("SECRET_KEY")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS")

# --- HTTPS & cookies (§20); TLS terminates at Caddy ---
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # Docker/Caddy health probes use plain HTTP
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=3600)  # raise once stable
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
