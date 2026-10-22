# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Phase 1 — Accounts, invites, public pages:
  - django-allauth: email login, mandatory verification, password reset, Google sign-in
    (hidden until `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are set); allauth pages restyled.
  - Invite-only signup: `/join/<code>/`, invite-only account and social adapters, email-locked,
    expiring, single-use invites, required terms/privacy consent checkbox.
  - Models `UserSettings`, `Invite`, `WaitlistEntry`, `ConsentRecord` (accounts) and `UsageQuota`
    (llm, model only); the signup handler creates settings, quota and consent rows.
  - Admin: bulk invite generation, copy links, email invite, waitlist approve/reject, user settings
    and quotas (also as inlines on the user).
  - Public pages: landing with a rate-limited waitlist form, privacy policy, terms, custom 403,
    404 and 500 pages.
  - App shell (desktop sidebar, mobile bottom nav), dashboard placeholder, settings → account page.
  - Branded HTML + text emails for verification, password reset and invites.
  - Rate limits: login/signup 10/min/IP, waitlist 5/hour/IP (django-ratelimit; shared DB cache
    in production).
  - ADR 0009. New dependencies: `django-allauth`, `django-ratelimit`.
- Phase 0 — Foundation and tooling:
  - Django 5 project `jobpilot` with split settings (`base`, `dev`, `test`, `prod`) driven by
    django-environ; `prod` fails fast and lists every missing required variable.
  - Custom `accounts.User` (email login, no username) created before the first migration.
  - `apps.core`: `TimeStampedModel`, `UserOwnedModel` with `for_user`, tenancy helpers,
    nh3 sanitise helpers, `/healthz` (shallow, and staff-only `?deep=1`).
  - Structured logging: JSON and console formatters, context and redaction filters,
    `log_event`, `RequestIDMiddleware` (`X-Request-ID`), `@logged_task`, optional Sentry.
  - Huey (SQLite storage) with the `core.heartbeat` periodic task.
  - Tailwind standalone CLI setup script, base templates, vendored HTMX and Alpine.
  - Dev `compose.yml` (Postgres 16 + Mailpit), multi-arch `docker/Dockerfile`.
  - Tooling: ruff, mypy + django-stubs, pytest (network blocked), pre-commit, GitHub Actions CI,
    Dependabot.
  - ADRs 0001–0008 in `docs/adr/`.
