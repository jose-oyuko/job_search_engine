# PROGRESS.md

Claude Code updates this file at the end of every phase (see `CLAUDE.md` §2.4).

## Current phase

**Phase 1 — Accounts, invites, public pages** (complete, awaiting owner review). Phase 0 is still awaiting merge; `phase/1-accounts` is branched from `phase/0-foundation`. Next: Phase 2, once the owner says to proceed.

## Phase status

| Phase | Name | Status | Completed on |
|---|---|---|---|
| 0 | Foundation and tooling | 🟨 Done, awaiting owner approval | 2026-10-21 |
| 1 | Accounts, invites, public pages | 🟨 Done, awaiting owner approval | 2026-10-22 |
| 2 | LLM core layer and usage metering | ⬜ Not started | |
| 3 | Profile, tracks and CV import | ⬜ Not started | |
| 4 | Source framework, ExpertWoka, ingestion | ⬜ Not started | |
| 5 | Manual job input | ⬜ Not started | |
| 6 | Matching, scoring and the job feed | ⬜ Not started | |
| 7 | Application kit generation and documents | ⬜ Not started | |
| 8 | Notifications: Telegram and email | ⬜ Not started | |
| 9 | Application tracker | ⬜ Not started | |
| 10 | Email job-alert ingestion | ⬜ Not started | |
| 11 | Privacy, security hardening, polish | ⬜ Not started | |
| 12 | Production deployment | ⬜ Not started | |

Legend: ⬜ Not started · 🟨 In progress · ✅ Complete (approved by owner)

## Things the owner must prepare (and when)

| Needed by | Item |
|---|---|
| Phase 1 | Google OAuth client ID/secret (can use a localhost redirect URI for dev) |
| Phase 2 | LLM API key + chosen fast/writer models + their prices for `LLM_PRICES_JSON` |
| Phase 3 | One or two of your real CVs (PDF/DOCX) to test import |
| Phase 8 | Telegram bot token + username (create via @BotFather) |
| Phase 10 | Dedicated Gmail inbox + app password; domain on Cloudflare with Email Routing |
| Phase 12 | Domain name, Oracle Cloud account + VM, transactional email provider, Sentry/Healthchecks (optional) |

## Session log

<!-- Claude Code: append an entry per phase:
### Phase N — <name> — <date>
- Summary:
- Notes for next session:
- Open questions:
-->

### Phase 0 — Foundation and tooling — 2026-10-21
- Summary: Django 5 project `jobpilot` (split settings, prod fail-fast), custom email-login
  `accounts.User`, `apps.core` (base models, `for_user`, tenancy helpers, sanitise, `/healthz`),
  structured logging (JSON/console, redaction, request IDs, `@logged_task`, optional Sentry),
  Huey + heartbeat task, Tailwind setup + base templates, compose, Dockerfile, CI, pre-commit,
  Dependabot, CHANGELOG, ADRs 0001-0008. Branch `phase/0-foundation`.
- Notes for next session:
  - Windows dev machine: `uv` lives in `%APPDATA%\Python\Python39\Scripts` (not on PATH); gitleaks
    via winget. Native Postgres instances occupy 5432/5433 locally, so `.env` uses `DB_PORT=5544`.
  - `tests/testapp` (installed only by test settings) holds test-only models; extend it for
    isolation tests. Run `makemigrations --check` with `DJANGO_SETTINGS_MODULE=jobpilot.settings.test`.
  - Tailwind is v4 (standalone); `static/css/app.css` is git-ignored and built locally/in Docker.
  - No CSP yet (django-csp is Phase 11); Inter font not self-hosted yet (system font stack).
  - `docker/entrypoint.sh` must stay LF (enforced via `.gitattributes`).
- Open questions: none.

### Phase 1 — Accounts, invites, public pages — 2026-10-22
- Summary: allauth (email + Google, mandatory verification, reset) behind invite-only adapters;
  models `UserSettings`, `Invite`, `WaitlistEntry`, `ConsentRecord`, `UsageQuota` (llm); signup
  service creates settings/quota/consents and claims the invite; admin for invites (bulk, links,
  email) and waitlist (approve/reject); landing + waitlist (rate-limited), privacy, terms, 403/404/500;
  app shell with sidebar and mobile bottom nav; branded emails; ADR 0009. Branch `phase/1-accounts`.
- Notes for next session:
  - Branch was cut from `phase/0-foundation` because `main` does not have Phase 0 yet; merge
    phase 0 first (or rebase) when reviewing.
  - Run the suite with `ENV_FILE=/nonexistent.env` (as CI does); the local `.env` sets
    `HEARTBEAT_INTERVAL_MINUTES=1`, which fails `test_heartbeat_is_scheduled_every_ten_minutes`.
  - When editing files from Python on Windows pass `encoding="utf-8"`; the default is cp1252.
  - TODOs left: `Profile` creation (Phase 3) and `InboundAddress` (Phase 10) in
    `accounts.services.complete_signup`; Telegram owner notification for waitlist (Phase 8);
    Settings → Privacy/notifications pages and export/delete (later phases).
  - Prod needs `python manage.py createcachetable` (added to `docker/entrypoint.sh`) for the shared
    rate-limit store.
- Open questions: privacy/terms text uses placeholders (`COMPANY_NAME`, `CONTACT_EMAIL`, governing
  law note); owner must fill them in and have the text reviewed before launch.
