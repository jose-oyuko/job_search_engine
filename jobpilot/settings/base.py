"""Settings shared by every environment. Environment-specific modules override this one."""

import contextlib
import json
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

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
# Shown on the legal pages; the owner replaces the placeholders before launch.
COMPANY_NAME = env.str("COMPANY_NAME", default="[Company name]")
CONTACT_EMAIL = env.str("CONTACT_EMAIL", default="hello@example.com")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",  # built-in widget templates, so TemplatesSetting can restyle them
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "huey.contrib.djhuey",
    "apps.core",
    "apps.accounts",
    "apps.llm",
    "apps.profiles",
    "apps.sources",
    "apps.jobs",
    "apps.matching",
    "apps.kits",
    "apps.notifications",
    "apps.tracker",
    "apps.inbound",
    "apps.pages",
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
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "jobpilot.urls"
# Form widgets render through the project template engine, so ours can override them.
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"
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
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "pages:dashboard"
LOGOUT_REDIRECT_URL = "pages:landing"

# allauth (§15.1): email login, mandatory verification, invite-only signup (adapters below).
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_SUBJECT_PREFIX = ""
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_ADAPTER = "apps.accounts.adapters.InviteOnlyAccountAdapter"
ACCOUNT_SIGNUP_FORM_CLASS = "apps.accounts.forms.SignupExtrasForm"
SOCIALACCOUNT_ADAPTER = "apps.accounts.adapters.InviteOnlySocialAccountAdapter"
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
# Google verifies emails, so an invited-then-existing user can sign in with Google too.
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
# Keep False: new Google users must pass through the signup form for the consent checkbox.
SOCIALACCOUNT_AUTO_SIGNUP = False
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_ONLY = False

INVITE_EXPIRY_DAYS = env.int("INVITE_EXPIRY_DAYS", default=14)
# Bump when the published text changes materially; stored on each ConsentRecord.
TERMS_VERSION = "2026-10"
PRIVACY_VERSION = "2026-10"

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

CV_MAX_UPLOAD_BYTES = env.int("CV_MAX_UPLOAD_BYTES", default=5 * 1024 * 1024)

# --- Scraping etiquette (§6.3) and job lifecycle (§7.5) ---
SCRAPER_USER_AGENT = env.str(
    "SCRAPER_USER_AGENT", default="Mozilla/5.0 (compatible; JobPilot/1.0; +https://example.com/bot)"
)
SCRAPER_DELAY_SECONDS = env.float("SCRAPER_DELAY_SECONDS", default=1.5)  # between requests per host
JOB_EXPIRY_DAYS = env.int("JOB_EXPIRY_DAYS", default=7)  # days unseen before a scraped job expires

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

# --- LLM (§11) ---
LLM_PROVIDER = env.str("LLM_PROVIDER", default="fake")  # fake | openai_compat
LLM_BASE_URL = env.str("LLM_BASE_URL", default="https://api.openai.com/v1")
LLM_API_KEY = env.str("LLM_API_KEY", default="")
LLM_MODEL_FAST = env.str("LLM_MODEL_FAST", default="")
LLM_MODEL_WRITER = env.str("LLM_MODEL_WRITER", default="")
LLM_LOG_BODIES = env.bool("LLM_LOG_BODIES", default=False)  # dev debugging only
LLM_SYSTEM_DAILY_USD_CAP = env.str("LLM_SYSTEM_DAILY_USD_CAP", default="1.00")
LLM_TIMEOUT_SECONDS = env.int("LLM_TIMEOUT_SECONDS", default=60)
# Send response_format=json_object; turn off for providers that reject it (we still extract JSON).
LLM_JSON_MODE = env.bool("LLM_JSON_MODE", default=True)
# Newer OpenAI models want "max_completion_tokens"; OpenRouter, Groq and others use "max_tokens".
LLM_MAX_TOKENS_PARAM = env.str("LLM_MAX_TOKENS_PARAM", default="max_tokens")
# Model -> {"input_per_mtok": x, "output_per_mtok": y}. Prices are never hardcoded (§11.3).
try:
    LLM_PRICES: dict[str, dict[str, float]] = json.loads(env.str("LLM_PRICES_JSON", default="{}"))
except json.JSONDecodeError as exc:
    raise ImproperlyConfigured("LLM_PRICES_JSON is not valid JSON") from exc
LLM_PROMPTS_DIR = BASE_DIR / "apps" / "llm" / "prompts"
LLM_FAKE_FIXTURES_DIR = BASE_DIR / "tests" / "fixtures" / "llm"
QUOTA_DEFAULT_MONTHLY_SCORES = env.int("QUOTA_DEFAULT_MONTHLY_SCORES", default=600)
QUOTA_DEFAULT_MONTHLY_KITS = env.int("QUOTA_DEFAULT_MONTHLY_KITS", default=40)
QUOTA_DEFAULT_MONTHLY_MANUAL = env.int("QUOTA_DEFAULT_MONTHLY_MANUAL", default=60)
QUOTA_DEFAULT_MONTHLY_USD = env.str("QUOTA_DEFAULT_MONTHLY_USD", default="3.00")

# --- Cache (used by django-ratelimit; prod swaps in a database cache shared by all workers) ---
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

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
TELEGRAM_BOT_USERNAME = env.str("TELEGRAM_BOT_USERNAME", default="")
# ptb: the real bot (needs the token). fake: records messages in memory (tests).
TELEGRAM_GATEWAY = env.str("TELEGRAM_GATEWAY", default="ptb")
GOOGLE_CLIENT_ID = env.str("GOOGLE_CLIENT_ID", default="")
GOOGLE_CLIENT_SECRET = env.str("GOOGLE_CLIENT_SECRET", default="")
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
SOCIALACCOUNT_PROVIDERS = (
    {
        "google": {
            "APP": {"client_id": GOOGLE_CLIENT_ID, "secret": GOOGLE_CLIENT_SECRET, "key": ""},
            "SCOPE": ["profile", "email"],
            "AUTH_PARAMS": {"access_type": "online"},
        }
    }
    if GOOGLE_ENABLED
    else {}
)
INBOUND_EMAIL_DOMAIN = env.str("INBOUND_EMAIL_DOMAIN", default="alerts.example.com")
INBOUND_IMAP_HOST = env.str("INBOUND_IMAP_HOST", default="imap.gmail.com")
INBOUND_IMAP_USER = env.str("INBOUND_IMAP_USER", default="")
INBOUND_IMAP_PASSWORD = env.str("INBOUND_IMAP_PASSWORD", default="")
# imap: the real mailbox (needs the credentials). fake: an in-memory mailbox (tests).
INBOUND_MAILBOX = env.str("INBOUND_MAILBOX", default="imap")
