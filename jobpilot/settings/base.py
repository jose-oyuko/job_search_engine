"""Settings shared by every environment. Environment-specific modules override this one."""

import contextlib
from pathlib import Path

import environ

from apps.core.logging import build_logging_config
from apps.core.sentry import init_sentry

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _ensure_dir(path: Path) -> None:
    """Create a runtime directory if possible. Failure is left to surface where it is used, so
    prod's missing-variable error is not hidden by an unrelated permissions error."""
    with contextlib.suppress(OSError):
        path.mkdir(parents=True, exist_ok=True)


env = environ.Env()
# ENV_FILE lets tests point at a missing file so a developer's .env cannot leak in.
_env_file = Path(env.str("ENV_FILE", default=str(BASE_DIR / ".env")))
if _env_file.is_file():
    environ.Env.read_env(_env_file)

# --- Core Django ---
SECRET_KEY = env.str("SECRET_KEY", default="insecure-dev-only-key")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
SITE_URL = env.str("SITE_URL", default="http://localhost:8000")
SITE_NAME = env.str("SITE_NAME", default="JobPilot")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "huey.contrib.djhuey",
    "apps.core",
    "apps.accounts",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "jobpilot.urls"
WSGI_APPLICATION = "jobpilot.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site",
            ],
        },
    },
]

# --- Database ---
# Postgres in dev/prod via DATABASE_URL; SQLite fallback keeps tooling (mypy, CI) working.
DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'var' / 'db.sqlite3'}"),
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth ---
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- i18n / time ---  Everything is stored in UTC; convert to the user's zone for display only.
LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files ---
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
_ensure_dir(STATIC_ROOT)  # WhiteNoise warns on every start if it is missing
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- Private files (never under MEDIA_URL; served only through ownership-checked views) ---
PRIVATE_MEDIA_ROOT = Path(env.str("PRIVATE_MEDIA_ROOT", default=str(BASE_DIR / "var" / "private")))

# --- Huey (§18) ---
HUEY_DB_PATH = Path(env.str("HUEY_DB_PATH", default=str(BASE_DIR / "var" / "huey.db")))
_ensure_dir(HUEY_DB_PATH.parent)
HUEY = {
    "name": "jobpilot",
    "huey_class": "huey.SqliteHuey",
    "filename": str(HUEY_DB_PATH),
    "immediate": False,
    "results": False,
    "consumer": {"workers": 4, "worker_type": "thread"},
}
HEARTBEAT_INTERVAL_MINUTES = env.int("HEARTBEAT_INTERVAL_MINUTES", default=10)

# --- Email (outbound) ---
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env.str("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="JobPilot <no-reply@example.com>")

# --- LLM (client arrives in Phase 2; the provider switch is needed from the start) ---
LLM_PROVIDER = env.str("LLM_PROVIDER", default="fake")

# --- Logging & observability (§21.1) ---
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO").upper()
LOG_FORMAT = env.str("LOG_FORMAT", default="console")
APP_RELEASE = env.str("APP_RELEASE", default="dev")
SERVICE_NAME = env.str("SERVICE_NAME", default="web")  # web | worker | bot
ENVIRONMENT = env.str("SENTRY_ENVIRONMENT", default="development")

LOGGING = build_logging_config(
    level=LOG_LEVEL,
    fmt=LOG_FORMAT,
    environment=ENVIRONMENT,
    release=APP_RELEASE,
    service=SERVICE_NAME,
)

SENTRY_DSN = env.str("SENTRY_DSN", default="")
init_sentry(
    dsn=SENTRY_DSN,
    environment=ENVIRONMENT,
    release=APP_RELEASE,
    service=SERVICE_NAME,
    traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
)

# Optional integrations: each disables itself when its variables are absent (checked in
# apps.core.apps.CoreConfig.ready, which logs one WARNING per disabled feature).
TELEGRAM_BOT_TOKEN = env.str("TELEGRAM_BOT_TOKEN", default="")
GOOGLE_CLIENT_ID = env.str("GOOGLE_CLIENT_ID", default="")
INBOUND_IMAP_USER = env.str("INBOUND_IMAP_USER", default="")
