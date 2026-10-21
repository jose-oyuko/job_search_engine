# PROGRESS.md

Claude Code updates this file at the end of every phase (see `CLAUDE.md` §2.4).

## Current phase

**Phase 0 — Foundation and tooling** (complete, awaiting owner review). Next: Phase 1, once the owner says to proceed.

## Phase status

| Phase | Name | Status | Completed on |
|---|---|---|---|
| 0 | Foundation and tooling | 🟨 Done, awaiting owner approval | 2026-10-21 |
| 1 | Accounts, invites, public pages | ⬜ Not started | |
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
