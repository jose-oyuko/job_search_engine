# PROGRESS.md

Claude Code updates this file at the end of every phase (see `CLAUDE.md` §2.4).

## Current phase

**Phase 10 — Email job-alert ingestion** (complete 2026-10-27, awaiting owner review; changes are uncommitted in the working tree). Next: Phase 11, once the owner says to proceed.

## Phase status

| Phase | Name | Status | Completed on |
|---|---|---|---|
| 0 | Foundation and tooling | 🟨 Done, awaiting owner approval | 2026-10-21 |
| 1 | Accounts, invites, public pages | ✅ Complete | 2026-10-22 |
| 2 | LLM core layer and usage metering | ✅ Complete | 2026-10-22 |
| 3 | Profile, tracks and CV import | ✅ Complete | 2026-10-22 |
| 4 | Source framework, ExpertWoka, ingestion | ✅ Complete | 2026-10-22 |
| 5 | Manual job input | ✅ Complete | 2026-10-22 |
| 6 | Matching, scoring and the job feed | ✅ Complete | 2026-10-23 |
| 7 | Application kit generation and documents | ✅ Complete | 2026-10-23 |
| 8 | Notifications: Telegram and email | ✅ Complete | 2026-10-26 |
| 9 | Application tracker | ✅ Complete | 2026-10-26 |
| 10 | Email job-alert ingestion | 🟨 Done, awaiting owner approval | 2026-10-27 |
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

### Phase 2 — LLM core layer and usage metering — 2026-10-22
- Summary: `apps.llm` now has the client (`call_structured`, `call_text`), fake and
  openai_compat providers, `LLMCall`/`LLMCache`, prompt loader (`apps/llm/prompts/*.md`),
  `wrap_untrusted`, quota + system-cap enforcement, Settings → Usage, an admin spend summary and
  the `llm_smoke` command. Work is uncommitted in the working tree on `main` (owner commits).
- Notes for next session:
  - Add new prompts as `apps/llm/prompts/<name>.md` with `version`, `task`, `tier` front-matter and
    `@@system` / `@@user` sections; bump `version` whenever the text changes (cache and logs use it).
  - Call sites pass `user=None` for system-paid work (counts against `LLM_SYSTEM_DAILY_USD_CAP`).
  - Quota groups: `scores` = `score_match`; `kits` = `generate_cv` (one per kit); `manual` =
    `extract_job` / `extract_page`. Phase 7 may refine how a kit is counted.
  - Cache keys must include the user when the result is user-specific.
  - Tests: use the `fake_llm` fixture (`.script.append(...)`) and `prompts_dir`; add fixtures in
    `tests/fixtures/llm/<task>.json`.
  - Run tests with `ENV_FILE=/nonexistent.env` (as CI does).
- Open questions: owner to run `llm_smoke` against a real provider (needs `LLM_*` and
  `LLM_PRICES_JSON` in `.env`).

### Phase 3 — Profile, tracks and CV import — 2026-10-22
- Summary: `apps.profiles` (models, services, serializers, forms, HTMX editor views, tracks, CV
  import with Huey parsing, onboarding wizard). All changes are uncommitted in the working tree on
  `main` (owner commits).
- Notes for next session:
  - Use `apps.profiles.services` for every profile write (it bumps `Profile.version`); never call
    `Model.objects.create` on profile models from views or tasks.
  - `serializers.serialize_profile(user)` and `profile_summary(user)` are the inputs for scoring
    (Phase 6) and kits (Phase 7); IDs look like `EXP-12`, `BUL-88`, `SKL-3`; `parse_ref()` reads them.
  - The wizard step registry is `apps.profiles.onboarding.STEPS`; Phases 8 and 10 append steps.
  - CV parsing needs the Huey worker (`run_huey`); without it an upload stays on "Reading your CV".
  - Tests that trigger parsing must use `django_capture_on_commit_callbacks(execute=True)`.
  - Test file builders are in `tests/files.py` (PDF, DOCX, scanned, encrypted, zip bomb).
  - Tests ignore E501 (pyproject per-file ignore); app code keeps the 100-column limit.
  - Reordering uses up/down buttons; SortableJS is not vendored yet (Phase 9 kanban).
- Open questions: none.

### Phase 4 — Source framework, ExpertWoka, ingestion — 2026-10-22
- Summary: `apps.sources` (framework, generic fetchers, ExpertWoka plugin, runs, health, scheduler,
  admin, status page, commands) and `apps.jobs` (Job pool, ingestion, pay/eligibility/scam/dedupe,
  expiry, LLM extraction). Changes are uncommitted in the working tree on `main` (owner commits).
- Notes for next session:
  - First time on a database: `python manage.py seed_sources`, then run `run_huey`. Try a source with
    `python manage.py test_source expertwoka --dry-run --limit 5` (this hits the real site).
  - ExpertWoka reality differs from the spec: JSON-LD only on country-restricted jobs, so the plugin
    parses HTML first (ADR 0012). Saved pages are in `tests/fixtures/html/expertwoka/`.
  - `ingest_raw_jobs` returns `new_job_ids` (canonical jobs only); Phase 6 must hook
    `matching.match_new_job` after extraction (see the TODO in `sources/services._enqueue_extraction`).
  - `Job.objects.visible_to(user)`, `.canonical()` and `.active()` are the querysets to use; duplicates
    (`duplicate_of` set) must never be matched or notified.
  - `is_eligible(job, country)` returns True/False/None; None means "eligibility unclear", not "no".
  - Failing sources only log now; the Telegram staff notification is a TODO for Phase 8.
  - New fetchers: write a plugin in `apps/sources/custom/` or a generic kind; see `docs/SOURCES.md`.
  - Tests use the `router` fixture (respx, any unmocked request fails) and `tests/source_helpers.py`.
  - Writing files from a bash heredoc with backslashes corrupts them on this machine (a backslash-n
    becomes a real newline); use the Write/Edit tools or build strings with `chr(92)`.
- Open questions: ExpertWoka's terms bar republishing their materials without permission; ask them
  before a public launch. Set a real contact URL in `SCRAPER_USER_AGENT`.

### Phase 5 — Manual job input — 2026-10-22
- Summary: add-a-job page (link or pasted text), SSRF-safe fetcher, source-parser reuse, JSON-LD and
  LLM paths, private text jobs, daily limit, simple job detail page, matching hook seam. Changes are
  uncommitted in the working tree on `main` (owner commits).
- Notes for next session:
  - Phase 6 must provide `apps.matching.services.match_job_for_user(user, job)`; manual input calls it
    through `apps/jobs/matching_hook.py` (create the Match, score it, count the quota) and the job
    detail page (`templates/jobs/detail.html`) should then show the score. The page is deliberately
    simple; Phase 6/7 replace it with the full detail page and kit panel.
  - Every user-supplied URL must go through `apps.core.ssrf.safe_fetch`; never call httpx directly
    on user input. Phase 10 (email alerts) and any future "open link" feature should reuse it.
  - Manual public jobs have `source=None`; `Job.objects.visible_to(user)` is the access rule and the
    detail view 404s on other users' private jobs.
  - `blocked_sites` list is `BLOCKED_DOMAINS` in `apps/jobs/manual.py`.
  - Tests: DNS is faked by patching `apps.core.ssrf.system_resolver`; pages are served at the pinned
    IP URL (`https://93.184.216.34/...`) with respx.
- Open questions: none.

### Phase 6 — Matching, scoring and the job feed — 2026-10-23
- Summary: `apps.matching` (rules, scoring, pipeline, feed, card actions, Search now), the job page
  match panel and report form, wiring from extraction and manual input. Changes are uncommitted in
  the working tree on `main` (owner commits).
- Notes for next session:
  - Hooks waiting for you: `apps/matching/hooks.py::after_score(match)` is where Phase 7 enqueues
    kit generation (`auto_kit_threshold`) and Phase 8 the instant notification
    (`instant_score_threshold`, waits up to 90 s for the kit). "Mark applied" only sets
    `Match.status`; Phase 9 must also create the Application (see `matching/views.py::applied`).
  - The job page (`templates/jobs/detail.html`) has no kit panel yet; Phase 7 adds it there.
  - Scores are stale when `scored_profile_version != profile.version`; never store that flag.
  - Anything that changes what the user wants must bump the profile version through
    `profiles.services`, which schedules the debounced rematch.
  - A track-less user gets no matches by design; the dashboard explains it.
  - Tests: `tests/matching_helpers.py` (`make_user`, `make_job`), `MatchFactory`; scoring uses the
    `fake_llm` fixture and `tests/fixtures/llm/score_match.json`. In tests `task.schedule(delay=...)`
    only queues (it does not run), so rematch tests call `services.rematch_user` directly.
  - The per-user "told once per month about the limit" notice from §10.3 belongs to Phase 8 (it
    needs a notification channel); the on-screen badge is done.
- Open questions: none.

### Phase 7 — Application kit generation and documents — 2026-10-23
- Summary: `apps.kits` (Kit model, pipeline, fabrication validator, style rules, PDF and Word
  rendering, kit panel on the job page, edit and re-render, versions, ownership-checked downloads,
  auto-kit hook). Changes are uncommitted in the working tree on `main` (owner commits).
- Notes for next session:
  - Hooks waiting for you: `apps/kits/hooks.py::after_generate(kit)` is where Phase 8 sends the
    "kit ready" message (Telegram documents, email link) and releases an instant match alert that
    was waiting for the kit (`after_score` in `apps/matching/hooks.py` starts the auto-kit; the
    notification part is still a TODO there). Use `kits.services.latest_ready_kit(user, job)` and
    `Kit.status` to see whether a kit exists or is in progress (queued/generating) for the 90 s wait.
  - Phase 9: when applying, link `Kit` via `kits.services.latest_ready_kit` (editable).
  - Documents never carry model-written facts: `documents.build_cv_context` reads names, companies,
    dates and institutions from the database by ID. Keep it that way.
  - Add a prompt change as a new `version` in `apps/llm/prompts/generate_*.md`; the style rules are in
    the prompts and `apps/kits/style.py` (keep both in sync).
  - PDF needs Pango. Docker image has it. On this Windows machine WeasyPrint cannot load (no GTK), so
    the 5 `@requires_pdf` tests skip and other kit tests use a stand-in PDF (`pdf_fallback` fixture).
    The real PDF tests were run in a Linux container instead:
    `docker run --rm -v "$(pwd -W):/src:ro" python:3.12-slim-bookworm bash -c '<apt install
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1 shared-mime-info rsync; pip
    install uv; rsync /src to /app excluding .venv; UV_PROJECT_ENVIRONMENT=/venv uv sync --locked;
    pytest tests/test_kits_*.py --no-cov>'`. To run them natively install the GTK runtime
    (`winget install tschoonj.GTKForWindows`, new terminal).
  - After editing `apps/kits/docx_templates.py` run `python manage.py build_doc_templates`; a test
    fails if the committed `.docx` files drift.
  - With `LLM_PROVIDER=fake` a kit falls back to the profile's own wording (the fake CV has no real
    IDs); use a real provider to judge tone.
  - Context-key gotcha: the job page merges the match panel and kit panel contexts; kit keys are
    prefixed `kit_*` where they could collide (`kit_stale`).
- Open questions: owner to generate one kit for a real job with a real provider and review the tone
  (acceptance criterion); report any prompt adjustments back so prompts can move to v2.

### Phase 8 — Notifications: Telegram and email — 2026-10-26
- Summary: `apps.notifications` (models, gateway, signing, pure schedule, services, bot, dispatcher,
  settings page, onboarding step 5), hooks from scoring, kits, sources and the waitlist. Changes are
  uncommitted in the working tree on `main` (owner commits).
- Notes for next session:
  - All sends go through `services.create_notification` + `services.deliver` (dedupe key, claim,
    retries). Add new kinds in `deliver`'s `_send`; keep the payload to IDs and render text at send time.
  - Phase 9: `bot_services.handle_callback` ("Applied ✓") and the dashboard button only set
    `Match.status`; create the Application there (TODOs in place) linked to
    `kits.services.latest_ready_kit`. `/status` has a TODO for applications by stage. The follow-up
    reminder (spec §13.5) should create `Notification(kind="followup_reminder", payload={"followup_text": ...})`
    rows with keys like `followup:<application>:<date>`; `_send_text` already delivers such a payload
    on Telegram only, so add email there if wanted.
  - The bot (`run_bot`) and worker must both run; the bot is a separate process like the worker.
  - Tests: `tg` fixture (FakeGateway, reset per test), `tests/notif_helpers.py` (`make_notif_user`,
    `scored_match`), `TELEGRAM_GATEWAY=fake` in test settings. The real `PtbGateway` is tested against
    a respx-mocked Bot API (needs a `getMe` mock too). `pdf_fallback` is still needed by tests that
    build kits on a machine without Pango.
  - Telegram URL buttons are omitted unless `SITE_URL` is public https (localhost is rejected).
- Open questions: owner to create a bot with @BotFather, set `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_BOT_USERNAME`, and try the flow end to end (the one acceptance item that needs the real
  Telegram service).

### Phase 9 — Application tracker — 2026-10-26
- Summary: `apps.tracker` (models, services, stats, board, detail, manual form, reminders task),
  wired into the feed, the job page and Telegram. Changes are uncommitted in the working tree on
  `main` (owner commits).
- Notes for next session:
  - `tracker.services.apply_to_job` is the only place an Application is created; call it from any new
    entry point (Phase 10 email-ingested jobs do not need it). `change_stage` is the only place a
    stage changes (it writes the event).
  - Follow-ups are `Notification(kind="followup_reminder")` with `payload={"application_id", "what"}`
    and dedupe keys `followup:<id>:<date>:<channel>` / `ghost:<id>:<channel>`; the text is built in
    `notifications.services._send_followup`.
  - Stats definitions are in `apps/tracker/stats.py` (docstring). Interview times are typed in the
    user's zone and stored in UTC (`InterviewForm`).
  - Drag and drop is `static/js/tracker.js` + vendored `Sortable.min.js`; it cannot be tested without a
    browser, so the server side (stage endpoint returns the board) is what the tests cover.
  - Tests: `tests/test_tracker_services.py` (`seed()` builds applications with history),
    `tests/test_tracker_views.py` (includes isolation).
- Open questions: owner to try the board in a browser (drag, dropdown on a narrow window) and the
  reminders; those are the parts tests cannot see.

### Phase 10 — Email job-alert ingestion — 2026-10-27
- Summary: `apps.inbound` (addresses, mailbox interface with IMAP and fake, routing, parsers,
  confirmation banner, settings page, onboarding step 6, retention task), `docs/EMAIL_SETUP.md`.
  Changes are uncommitted in the working tree on `main` (owner commits).
- Notes for next session:
  - The owner has not run this against a real inbox. Real LinkedIn emails may need tuning of
    `apps/inbound/parsers/linkedin.py` (fixtures in `tests/fixtures/emails/` mimic two layouts). Ask
    for real `.eml` samples; add dedicated parsers (BrighterMonday, Fuzu, MyJobMag) the same way,
    register them in `parsers/__init__.py::choose_parser`.
  - `Match.user_added` is how "the user put this job in front of the rules" is remembered; any new
    entry point that forces a match goes through `matching.services.match_job_for_user`.
  - Phase 11 retention: `inbound.clear_old_bodies` already clears email text after 30 days; the
    spec's `core.retention_cleanup` can call `inbound.services.clear_old_bodies`. Phase 11's export
    and delete must include `InboundAddress`, `InboundEmail` and `ForwardingConfirmation`.
  - Tests: `tests/inbound_helpers.py` (`load_eml`, `mailbox_with`), the shared fake mailbox is
    `apps.inbound.mailbox.fake_mailbox()` (`INBOUND_MAILBOX=fake` in test settings).
  - The onboarding wizard is now 6 steps (`cv, review, target, location, notifications, alerts`).
- Open questions: owner to do the real forwarding test in `docs/EMAIL_SETUP.md` section 5.
