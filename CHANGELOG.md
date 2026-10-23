# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Phase 2 — LLM core layer and usage metering:
  - `apps.llm.client`: `call_structured` / `call_text` with prompt rendering, result cache,
    quota checks before any provider call, schema validation with a single repair retry
    (`LLMOutputError` after that), cost from `LLM_PRICES_JSON`, and one `LLMCall` row per call.
  - Providers: `fake` (fixtures in `tests/fixtures/llm/`, schema-valid defaults) and
    `openai_compat` (httpx, timeouts, retries on 5xx/429, optional JSON mode).
  - Models `LLMCall` and `LLMCache`; quota enforcement against `UsageQuota` (monthly counters
    per task group plus the USD cap) and a system daily cap (`LLM_SYSTEM_DAILY_USD_CAP`).
  - Prompt loader with front-matter versioning; `wrap_untrusted()` for untrusted text.
  - Settings → Usage page (`/app/settings/usage/`) and an LLM spend summary in the admin.
  - `llm_smoke` management command for a manual check against the configured provider.
  - New settings: `LLM_TIMEOUT_SECONDS`, `LLM_JSON_MODE`, `LLM_MAX_TOKENS_PARAM`.
  - ADR 0010. No new dependencies.

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
- Phase 3 — Profile, tracks and CV import:
  - `apps.profiles`: `Profile`, `Skill`, `Track`, `Experience`, `ExperienceBullet`, `Project`,
    `Education`, `Certification`, `CVImport`; one default track per user and unique skill names
    are enforced in the database. Every change goes through `services.py`, which bumps
    `Profile.version`.
  - Profile editor at `/app/profile/` with HTMX inline add, edit, delete and up/down reordering,
    a profile-strength meter with tips, and job targets (tracks) at `/app/profile/tracks/`.
  - CV import: PDF/DOCX upload (extension + magic bytes, 5 MB, decompression-bomb check, random
    private file names), text extraction (pypdf, python-docx), friendly errors for scanned or
    password-protected files, `parse_cv` prompt v1 run in a Huey task, a review screen, and apply
    with replace or merge. Unsupported metrics and skills are dropped after parsing.
  - Onboarding wizard at `/welcome/` (steps: CV, review, target, location and timezone), built
    from an extensible step registry; `/app/` redirects there until onboarding is complete.
  - `profiles/serializers.py`: stable IDs (`EXP-12`, `BUL-88`, ...), a deterministic profile
    serialiser and a compact summary cached per profile version.
  - Private, ownership-checked download of uploaded CVs.
  - New setting: `CV_MAX_UPLOAD_BYTES`. New dependencies: `pypdf`, `python-docx`, `tzdata`.
  - ADR 0011.
- Phase 4 — Source framework, ExpertWoka, ingestion:
  - `apps.sources`: `Source`, `SourceRun`, `RawJob`/`BaseSource` (two-phase fetching), a registry
    with lazy auto-import, a polite HTTP helper (retries, 429 handling, path-only logging) and
    generic fetchers `jsonld`, `json_api`, `rss`, `ats` (Greenhouse and Lever, endpoints verified
    against the live APIs) and `llm_extract`.
  - ExpertWoka plugin, built from real saved pages (HTML-first, because JSON-LD exists only on
    country-restricted jobs); fixtures in `tests/fixtures/html/expertwoka/`.
  - `apps.jobs`: `Job` (shared pool) and `JobReport`; ingestion with upsert, sanitising, pay parsing,
    eligibility parsing, cross-source dedupe, scam heuristics, expiry and the once-per-job LLM
    `extract_job` task (`JobExtraction` schema and prompt).
  - Scheduler (`sources.tick` every 5 minutes), per-source locks, health tracking
    (ok, degraded, failing), daily `jobs.expire_stale` with 404/410 re-checks.
  - Admin for sources (run now, health, recent runs) and jobs (search, filters, raw JSON);
    staff status page at `/app/staff/status/`.
  - Commands `seed_sources` and `test_source <name> [--dry-run] [--limit N] [--json]`.
  - `docs/SOURCES.md`, ADR 0012. New settings: `SCRAPER_DELAY_SECONDS`, `JOB_EXPIRY_DAYS`
    (`SCRAPER_USER_AGENT` is now wired in). New dependencies: `selectolax`, `defusedxml`, `certifi`.
- Phase 5 — Manual job input:
  - `/app/jobs/add/` with two tabs (paste a link, paste a description) and a simple job page at
    `/app/jobs/<id>/` (public jobs and the user's own private jobs only).
  - URL path in spec order: blocked sites (LinkedIn, Indeed, Glassdoor, ...) show "paste the
    description instead"; a configured source's own parser (ExpertWoka URLs); JSON-LD without the
    LLM; otherwise `extract_page` on the user's manual-extraction allowance.
  - `apps/core/ssrf.py`: SSRF-safe fetching (public addresses only, connection pinned to the
    validated IP, validated redirects, 2 MB and 20 s caps, HTML only).
  - Pasted text creates a private job via `extract_job` (`JobExtraction` gained optional `title`
    and `company`); a repeat paste returns the same job at no charge.
  - 20 additions per user per day across both tabs; usage shows on Settings → Usage.
  - `apps/jobs/matching_hook.py`: a no-op until Phase 6 provides the matching service.
  - ADR 0013. No new dependencies.
- Phase 6 — Matching, scoring and the job feed:
  - `apps.matching`: `Match` model, a pure rule filter (eligibility, work type and location, pay,
    exclusions, relevance) that records plain-words reasons, and LLM scoring (`MatchScore` schema,
    `score_match` prompt v1) with verdicts derived from the score.
  - Pipeline: `match_new_job` after extraction, `rematch_user` (debounced 30 s) on any profile or
    track change, per-match scoring tasks, lazy refresh of stale or unscored scores, a "Rescore"
    action, and "Score limit reached" with no model call when the allowance is used up.
  - Dashboard feed at `/app/`: ranked by score, filters (track, verdict, status, work type,
    company or title, my country only, new), HTMX "load more", cards with save, dismiss (with a
    reason) and mark applied, and badges for eligibility, scam signals, expired jobs, stale scores
    and the score limit.
  - Job page: score, strengths and gaps, "why this job was shown", source and alternate links,
    "Report this listing".
  - "Search now": runs stale-enough sources and rescoring, polled progress, one use per 10 minutes.
  - Phase 5 manual jobs are matched and scored for the user who added them.
  - ADR 0014. No new dependencies.

### Fixed
- `log_event` no longer raises when a field is named like a `LogRecord` attribute (`created`,
  `name`, `module`); the field gets a `_field` suffix.
- Log lines now keep an explicit `user_id=` field instead of overwriting it with the request
  context (it was always null for signup events).
