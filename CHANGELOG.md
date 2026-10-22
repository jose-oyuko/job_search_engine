# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
