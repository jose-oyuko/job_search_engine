# JobPilot — Project Specification

> Working name: **JobPilot**. Python package: `jobpilot`. Rename consistently if the owner changes it.
>
> This document is the single source of truth for what to build. `CLAUDE.md` explains *how* to work (the phase protocol). `PROGRESS.md` tracks status.

---

## Table of contents

1. Overview, goals and constraints
2. Tech stack (decided)
3. Architecture
4. Repository layout
5. Data model
6. Job sources framework
7. Ingestion: normalisation, deduplication, eligibility
8. Manual job input (URL and pasted text)
9. Email job-alert ingestion
10. Matching: rule filter + LLM scoring
11. LLM layer
12. Application kit generation (CV, cover letter, interview prep)
13. Notifications (Telegram, email, digests)
14. Application tracker
15. Accounts, invites and onboarding
16. Multi-user isolation
17. Frontend and UI
18. Background tasks and schedules
19. Testing strategy
20. Security, privacy and compliance
21. Coding conventions, logging and engineering practices
22. Configuration and environment variables
23. **Implementation phases** ← build order
24. DevOps and deployment
25. Backlog (not in scope yet)

---

## 1. Overview, goals and constraints

### 1.1 What the product does
JobPilot helps job seekers get hired faster with less effort. The user's only manual job is to **apply**. Everything else is automated or one click away:

1. **Find jobs** automatically from configured sources: aggregators like ExpertWoka, AI-training platforms like micro1 and Turing, job boards, and job-alert emails the user forwards (LinkedIn etc.). Users can also add jobs manually by pasting a URL or a job description.
2. **Filter and score** each job against the user's profile, so only relevant, eligible jobs reach them.
3. **Alert** the user via Telegram and/or email: instantly for strong matches, or as a digest on a schedule the user controls.
4. **Generate an application kit** per job: a tailored CV, a cover letter, likely interview questions with suggested answers, and screening-question answers. Downloadable as PDF and DOCX from the dashboard.
5. **Track applications** through stages (applied → interview → offer…), with follow-up reminders and outcome data kept for future insights.

Jobs can be remote, onsite or hybrid. Many target roles are remote AI-training gigs (micro1, Turing, TELUS Digital…) that **fill fast and close**, so detection speed matters.

### 1.2 Who uses it (now and later)
- **Now:** the owner (based in Nairobi, Kenya) plus a small group of **invited** friends. Invite-only, but it must look and behave like a real, professional product: custom domain, Google sign-in, branded emails, landing page, privacy policy.
- **Later:** possibly a public business. The design must not block that, but we do **not** build business features (billing, public signup, analytics at scale) yet.

### 1.3 Constraints
- **Solo developer.** Prefer few moving parts. No microservices, Kafka, Kubernetes, Redis, Celery or separate vector databases.
- **Hosting must fit free tiers.** Target is one Oracle Cloud Always Free ARM VM (currently 2 OCPU / 12 GB on the free allocation), running Docker Compose. The only fixed cost is a domain name.
- **LLM usage is paid** (pay-as-you-go API) and must be **metered and capped per user**.
- **Low resource usage.** The whole stack should run comfortably in under 2 GB of RAM.
- **Mobile-first UI.** Most users will be on phones, often on mobile data.
- **Kenya context:** user country defaults to Kenya (KE). Eligibility filtering by country is essential, since many platforms exclude certain countries. Personal data handling should respect the Kenya Data Protection Act 2019 (§20).

### 1.4 Non-goals for now
Public signup, payments, WhatsApp, a native mobile app, Gmail/Outlook OAuth mailbox reading, auto-applying to jobs, scraping LinkedIn pages, the recommendations engine, and PostgreSQL row-level security. All of these are in the backlog (§25).

---

## 2. Tech stack (decided)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Package manager | **uv** | `pyproject.toml` + `uv.lock` |
| Web framework | **Django 5.x** (latest stable 5.x) | |
| Database | **PostgreSQL 16** in dev and prod; **SQLite** for tests | ORM only |
| Background jobs and schedules | **Huey** (`huey.contrib.djhuey`) with **SqliteHuey** storage | No Redis. The Huey SQLite file sits on a volume shared by the web, worker and bot containers |
| Auth | **django-allauth** | Email + password with mandatory verification, Google sign-in, invite-only |
| Frontend | Django templates + **HTMX** + **Alpine.js** + **Tailwind CSS** (standalone CLI, no Node) | **SortableJS** for the kanban board |
| Static files | **WhiteNoise** | |
| HTTP client | **httpx** | |
| HTML parsing | **selectolax** | |
| HTML sanitising | **nh3** | |
| Data validation and LLM schemas | **pydantic v2** | |
| LLM providers | Own thin wrapper (§11) over an **OpenAI-compatible** HTTP API (works with OpenAI, OpenRouter, Groq and others), plus a **Fake** provider for tests | Provider and models set via env |
| PDF parsing (CV import) | **pypdf** (fall back to **pdfplumber** only if needed) | |
| DOCX parsing (CV import) | **python-docx** | |
| PDF generation | **WeasyPrint** (HTML/CSS → PDF) | Needs Pango system libs in Docker |
| DOCX generation | **docxtpl** (Jinja-in-Word templates) | |
| Email ingestion | **imap-tools** | Reads one dedicated inbox (§9) |
| Outbound email | Django SMTP backend pointed at a transactional provider's SMTP (Resend, Brevo, Mailgun or similar) | Provider chosen by owner via env |
| Telegram | **python-telegram-bot** v21+ (async), **long polling** | Runs as a management command |
| Rate limiting | **django-ratelimit** | |
| Security headers | Django security settings + **django-csp** | |
| Settings / env | **django-environ** | |
| Error tracking | **sentry-sdk** | Only if `SENTRY_DSN` is set |
| App server | **gunicorn** | |
| Reverse proxy and TLS | **Caddy** | Auto HTTPS |
| Tests | **pytest**, **pytest-django**, **factory-boy**, **respx** (httpx mocking), **time-machine** | |
| Lint / format | **ruff** (including the `S` security rules) | + `pre-commit` |
| Type checking | **mypy** + **django-stubs** | Non-strict globally, strict on `services.py` and pure-logic modules |
| Dependency / secret scanning | **pip-audit** (CI), **gitleaks** (pre-commit + CI), **Dependabot** (or Renovate) | |
| Logging | stdlib `logging` + a small JSON formatter in `apps/core/logging.py` | No extra dependency (§21.1) |
| Dev only | **django-debug-toolbar**, **Mailpit** (local SMTP catcher) | |

---

## 3. Architecture

### 3.1 High-level flow

```
                    ┌──────────────────────── shared (global) ───────────────────────┐
 Job sources ──►  Fetchers ──► Ingestion (normalise, dedupe, sanitise) ──► Job pool
 (ExpertWoka,                                                                   │
  micro1, …,       Manual input (URL / pasted text) ─────────────────────────►  │
  email alerts) ─► Email ingestion (per-user forwarding addresses) ─────────►  │
                    └────────────────────────────────────────────────────────────┘
                                                                                │
                    ┌──────────────────────── per user ──────────────────────────┐
                    │  Rule filter (eligibility, pay, keywords, work type)        │
                    │        │                                                    │
                    │  LLM scoring (0–100 + strengths/gaps)  ◄── Master profile   │
                    │        │                                        │           │
                    │  Match ──► Notifications (instant / digest)     │           │
                    │        │                                        │           │
                    │  Kit generator (CV, cover letter, Q&A) ◄────────┘           │
                    │        │                                                    │
                    │  User applies ──► Application tracker ──► outcomes          │
                    └─────────────────────────────────────────────────────────────┘
```

### 3.2 Key architectural decisions
1. **Jobs are shared; everything else is per user.** Each source is fetched once for all users and stored in one `Job` table. Matching, scoring, kits, notifications and applications are per user. The exception is a job a user adds manually from pasted text: it is **private** to that user (`Job.visibility = private`).
2. **Fetching frequency is independent of notification frequency.** The system fetches sources on their own intervals (default 20 minutes). Each user chooses how often they're *notified*.
3. **One service layer.** Each Django app has `services.py` holding the business logic. Views, Huey tasks, management commands and the Telegram bot only call services. This keeps logic testable and lets us add a JSON API later without rewriting anything.
4. **Plugin-based sources.** New job sites are added by configuring a generic fetcher in the admin, or by dropping a single file into `sources/custom/` (§6).
5. **LLM calls go through one client** that enforces quotas, validates structured output, and logs cost per user (§11).
6. **Deterministic first, LLM second.** Cheap rule-based filtering runs before any LLM call, to keep costs low.

### 3.3 Processes (containers in production)

| Process | Command | Role |
|---|---|---|
| `web` | `gunicorn jobpilot.wsgi` | Dashboard, admin, landing pages |
| `worker` | `python manage.py run_huey` | Fetching, ingestion, scoring, kit generation, notifications, email polling, backups, retention |
| `bot` | `python manage.py run_bot` | Telegram bot (long polling) |
| `db` | PostgreSQL 16 | Data |
| `caddy` | Caddy 2 | HTTPS + reverse proxy (production only) |

`web`, `worker` and `bot` share a volume for the Huey SQLite file and for private media.

---

## 4. Repository layout

```
jobpilot/                         # repo root
├── CLAUDE.md
├── PROJECT_SPEC.md
├── PROGRESS.md
├── README.md
├── pyproject.toml / uv.lock
├── .env.example
├── manage.py
├── jobpilot/                     # Django project package
│   ├── settings/
│   │   ├── base.py
│   │   ├── dev.py
│   │   ├── test.py               # SQLite, Fake LLM, eager Huey, no network
│   │   └── prod.py
│   ├── urls.py
│   ├── wsgi.py
│   └── huey_config.py            # (optional) shared Huey helpers
├── apps/
│   ├── core/                     # shared utilities, base models, tenancy, health
│   │   ├── models.py             # TimeStampedModel, UserOwnedModel + manager
│   │   ├── tenancy.py            # for_user helpers, OwnedObjectMixin for views
│   │   ├── sanitize.py           # nh3 wrappers
│   │   ├── storage.py            # private file storage + serving helpers
│   │   └── views.py              # /healthz
│   ├── llm/                      # LLM client, providers, prompts, usage + quotas
│   │   ├── client.py
│   │   ├── providers/{base.py,openai_compat.py,fake.py}
│   │   ├── prompts/              # versioned prompt templates (*.md / *.txt)
│   │   ├── schemas.py            # pydantic output schemas
│   │   ├── models.py             # LLMCall, UsageQuota
│   │   └── services.py
│   ├── accounts/                 # User, UserSettings, Invite, Waitlist, Consent
│   ├── profiles/                 # Profile, Track, Experience, … , CVImport
│   ├── sources/                  # Source, SourceRun, fetchers, registry
│   │   ├── base.py               # RawJob, BaseSource
│   │   ├── registry.py
│   │   ├── generic/{jsonld.py,ats.py,json_api.py,rss.py,llm_extract.py}
│   │   └── custom/               # one file per custom site plugin
│   ├── jobs/                     # Job pool, ingestion, dedupe, eligibility, manual input
│   ├── matching/                 # Match, rule filter, scoring
│   ├── kits/                     # Kit, generation, validation, rendering
│   │   └── templates_docs/       # HTML (PDF) + .docx templates
│   ├── tracker/                  # Application, ApplicationEvent, Interview
│   ├── notifications/            # Notification, TelegramLink, digests, bot
│   ├── inbound/                  # InboundAddress, InboundEmail, parsers, IMAP poller
│   └── pages/                    # landing, privacy, terms, error pages
├── templates/                    # base.html, partials/, app templates
├── static/
│   ├── src/input.css             # Tailwind input
│   ├── css/app.css               # built (git-ignored in dev, built in Docker)
│   └── js/                       # htmx, alpine, sortable (vendored, pinned)
├── tests/                        # cross-app tests, isolation tests, fixtures/
│   └── fixtures/
│       ├── html/                 # saved pages from sources
│       ├── emails/               # saved .eml job-alert emails
│       └── llm/                  # canned Fake-LLM responses per task
├── bin/tailwind                  # standalone Tailwind binary (git-ignored; script downloads it)
├── scripts/                      # setup_tailwind.sh, backup.sh, restore.sh
├── docker/
│   ├── Dockerfile
│   ├── Caddyfile
│   └── entrypoint.sh
├── compose.yml                   # dev: db + mailpit (+ optional app)
├── compose.prod.yml              # prod: caddy, web, worker, bot, db
├── docs/
│   ├── DEPLOYMENT.md             # written in Phase 12
│   └── SOURCES.md                # how to add a new source (Phase 4)
└── .github/workflows/
    ├── ci.yml
    └── deploy.yml
```

Each app follows the same internal shape: `models.py`, `services.py`, `tasks.py` (Huey), `views.py`, `urls.py`, `forms.py`, `admin.py`, `templates/<app>/`, `tests/`.

---

## 5. Data model

The notation below is a guide, not final code. Use sensible field types, `db_index` where queries need it, and `TextChoices` for enums. All models get `created_at` / `updated_at` via `TimeStampedModel`.

### 5.1 Base classes (`apps/core/models.py`)
- `TimeStampedModel` (abstract): `created_at`, `updated_at`.
- `UserOwnedModel` (abstract): `user = FK(settings.AUTH_USER_MODEL, on_delete=CASCADE, db_index=True)`, with a default manager exposing `.for_user(user)`. **Every per-user model inherits this.**

### 5.2 accounts
- **User** (custom, `AbstractUser` subclass, created in Phase 0 *before the first migration*):
  - `email` unique, used as the login identifier;
  - `username` optional or removed;
  - `full_name`.
- **UserSettings** (OneToOne with User, created automatically on signup):
  - `timezone` (default `Africa/Nairobi`), `country` (ISO-2, default `KE`);
  - `notify_mode`: `instant | hourly | daily | off` (default `instant`);
  - `digest_time` (local time, default 08:00);
  - `quiet_hours_start`, `quiet_hours_end` (nullable);
  - `instant_score_threshold` (default 75), `auto_kit_threshold` (default 85);
  - `notify_telegram` bool, `notify_email` bool;
  - `last_digest_sent_at`, `last_manual_search_at`;
  - `share_anonymous_outcomes` bool (default False; for the future insights feature);
  - `onboarding_completed` bool.
- **Invite**: `code` (unique, random URL-safe), `email` (optional, locks the invite to that email), `created_by` FK User, `expires_at`, `used_by` FK User (nullable), `used_at`, `note`.
- **WaitlistEntry**: `email`, `name`, `reason` (text), `status`: `pending | invited | rejected`, `invite` FK (nullable).
- **ConsentRecord**: `user`, `kind` (`terms`, `privacy`, `outcome_sharing`), `version`, `granted` bool, `at`.

### 5.3 profiles
- **Profile** (OneToOne User): `headline`, `summary`, `phone`, `public_email`, `location_text`, `links` (JSON list of `{label, url}`), `years_experience` (nullable), `version` (int; incremented on any profile change and used to invalidate scores and kits).
- **Track** (UserOwned): a target career path. A user can have several; one is the default.
  - `name` (e.g. "IT support", "Backend developer", "AI training generalist"), `is_default`;
  - `target_titles` (JSON list), `include_keywords` (JSON list), `exclude_keywords` (JSON list);
  - `work_types` (JSON list from `remote`, `onsite`, `hybrid`), `locations` (JSON list of cities/countries for onsite);
  - `min_pay_amount` (decimal, nullable), `min_pay_currency` (default USD), `min_pay_period` (`hour | month | year`);
  - `employment_types` (JSON list from `full_time`, `part_time`, `contract`, `freelance`, `gig`);
  - `excluded_companies` (JSON list);
  - `cv_emphasis` (text: free-form guidance for the kit generator, e.g. "lead with infrastructure and support experience").
- **Experience** (UserOwned): `title`, `company`, `location`, `employment_type`, `start_date`, `end_date` (nullable means current), `description`, `order`.
- **ExperienceBullet** (UserOwned): `experience` FK, `text`, `metrics` (text, optional: the factual numbers, e.g. "reduced ticket backlog 40%"), `skills` (M2M Skill), `order`.
- **Project** (UserOwned): `name`, `role`, `url`, `description`, `technologies` (M2M Skill), `start_date`, `end_date`, `highlights` (JSON list of strings).
- **Education** (UserOwned): `institution`, `qualification`, `field`, `start_date`, `end_date`, `grade`, `notes`.
- **Certification** (UserOwned): `name`, `issuer`, `issued_on`, `expires_on`, `credential_url`, `status` (`earned | in_progress`).
- **Skill** (UserOwned): `name`, `category` (`technical | tool | soft | language | domain`), `level` (`basic | intermediate | advanced | expert`), `years` (nullable).
- **CVImport** (UserOwned): `file` (private storage), `status` (`uploaded | parsing | parsed | applied | failed`), `parsed_data` (JSON), `error`, `applied_at`.

Each profile item has a stable primary key. The kit generator references these IDs to prove every CV claim comes from the profile (§12.5).

### 5.4 sources
- **Source**:
  - `name` (slug, unique), `display_name`;
  - `kind`: `jsonld | ats | json_api | rss | llm_extract | custom`;
  - `config` (JSON; schema depends on kind, see §6.4);
  - `interval_minutes` (default 20), `enabled`;
  - `last_run_at`, `last_success_at`, `consecutive_failures`;
  - `health`: `ok | degraded | failing` (computed);
  - `notes`.
- **SourceRun**: `source` FK, `started_at`, `finished_at`, `trigger` (`schedule | manual | admin`), `ids_seen`, `new_jobs`, `updated_jobs`, `errors` (JSON list), `success` bool.

### 5.5 jobs
- **Job** (global, not UserOwned):
  - `source` FK (nullable for manual and email jobs), `external_id` (unique together with source, when source is set);
  - `origin`: `scraped | manual_url | manual_text | email`;
  - `visibility`: `public | private`, plus `owner` FK User (nullable; set when private);
  - `url` (listing URL), `apply_url`, `alternate_urls` (JSON list: same job seen elsewhere);
  - `title`, `company`, `company_platform` (e.g. "micro1" when a job is posted by micro1 on behalf of a client);
  - `description_html` (sanitised), `description_text`;
  - `pay_text` (raw), `pay_min`, `pay_max` (decimals, nullable), `pay_currency`, `pay_period` (`hour | task | day | week | month | year | one_time | unknown`);
  - `locations_raw` (JSON list of strings as found), `eligible_countries` (JSON list of ISO-2 codes), `eligible_regions` (JSON list of region codes), `is_global` bool, `eligibility_known` bool;
  - `work_type` (`remote | onsite | hybrid | unknown`), `employment_type`;
  - `posted_at`, `first_seen_at`, `last_seen_at`;
  - `status`: `active | expired | unknown`;
  - `dedupe_key` (indexed), `duplicate_of` FK self (nullable);
  - `extracted` (JSON; LLM-extracted structure: required skills, nice-to-have skills, seniority, summary, responsibilities, requirements, red flags; see §11.6);
  - `extracted_at`, `scam_flags` (JSON list), `raw` (JSON: original payload).
- **JobReport** (UserOwned): `job` FK, `reason` (`scam | expired | wrong_info | other`), `note`. Used by "report this listing".

### 5.6 matching
- **Match** (UserOwned): `job` FK, `track` FK (nullable), unique together on (user, job).
  - `rule_pass` bool, `rule_reasons` (JSON list);
  - `score` (int, nullable), `verdict` (`strong | good | weak | poor`);
  - `strengths` (JSON list), `gaps` (JSON list), `summary` (short text);
  - `scored_profile_version` (int; if it differs from the current profile version, the score is stale);
  - `status`: `new | notified | seen | saved | dismissed | applied`;
  - `dismiss_reason` (`not_relevant | pay_too_low | location | seniority | company | other`);
  - `notified_at`, `scored_at`, `llm_model`.

### 5.7 kits
- **Kit** (UserOwned):
  - `job` FK, `match` FK (nullable), `track` FK (nullable), `version` (int, per user+job);
  - `status`: `queued | generating | ready | failed`, `error`;
  - `instruction` (text; the user's regeneration instruction, if any);
  - `cv_data` (JSON: structured CV, schema in §12.3), `cover_letter_text`;
  - `interview_qa` (JSON list), `screening_answers` (JSON list);
  - `validation_report` (JSON: fabrication-check results);
  - `cv_pdf`, `cv_docx`, `cover_letter_pdf`, `cover_letter_docx` (private files);
  - `template` (string, default `classic`), `profile_version` (int);
  - `user_edited` bool. Set when the user edits text in the UI; re-rendering uses the edited data.

### 5.8 tracker
- **Application** (UserOwned): `job` FK, `kit` FK (nullable: which kit version was used), `track` FK (nullable).
  - `stage`: `saved | applied | screening | interview | offer | hired | rejected | withdrawn | ghosted`;
  - `applied_at`, `last_stage_change_at`, `next_followup_at`;
  - `channel` (where they applied: platform name, email, referral…), `notes`;
  - `outcome_reason` (text, optional), `salary_offered` (text, optional);
  - unique together on (user, job).
- **ApplicationEvent** (UserOwned): `application` FK, `from_stage`, `to_stage`, `at`, `note`. Created on every stage change through the service layer. This is the data for future insights.
- **Interview** (UserOwned): `application` FK, `scheduled_at`, `kind` (`ai_screen | phone | technical | panel | final | other`), `notes`, `questions_asked` (text), `outcome`.

### 5.9 notifications
- **TelegramLink** (OneToOne User): `chat_id` (string), `username`, `linked_at`, `active` bool.
- **TelegramLinkToken** (UserOwned): `token` (unique, random), `expires_at`, `used_at`.
- **Notification** (UserOwned):
  - `kind`: `instant_match | digest | kit_ready | followup_reminder | source_health | system`;
  - `channel`: `telegram | email`;
  - `payload` (JSON), `status` (`pending | sent | failed | skipped`);
  - `sent_at`, `error`;
  - `dedupe_key` (unique together with user, so the same alert is never sent twice);
  - `matches` (M2M Match: which matches this notification covered).

### 5.10 inbound
- **InboundAddress** (UserOwned): `token` (unique, short random, e.g. `u-7f3k9a`), `active`. The full address is `<token>@<INBOUND_EMAIL_DOMAIN>`.
- **InboundEmail** (UserOwned): `address` FK, `message_id` (unique), `from_address`, `subject`, `received_at`, `body_text`, `body_html` (raw; deleted after the retention period), `parser` (which parser handled it), `status` (`received | parsed | ignored | failed`), `jobs_found` (int), `error`.
- **ForwardingConfirmation** (UserOwned): `provider` (`gmail`), `confirmation_url`, `confirmation_code`, `received_at`, `dismissed` bool.

### 5.11 llm
- **LLMCall**:
  - `user` FK (nullable for system work like shared job extraction);
  - `task`: `extract_job | score_match | parse_cv | generate_cv | generate_cover_letter | generate_interview | generate_screening | extract_email_jobs | extract_page`;
  - `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd` (decimal), `latency_ms`;
  - `success` bool, `error`, `cache_hit` bool, `prompt_version`, `created_at`.
  - **Do not store prompt or response bodies in production.** Store them only when `LLM_LOG_BODIES=True`, for dev debugging.
- **UsageQuota** (OneToOne User; created with defaults): `monthly_scores`, `monthly_kits`, `monthly_manual_extractions`, `monthly_cost_usd_cap`. Defaults come from settings and can be edited in the admin.

---

## 6. Job sources framework

### 6.1 Responsibility boundary
A fetcher's only job: **talk to one site and return standard `RawJob` objects.** It does not dedupe, filter, score or save. Ingestion (§7) does those things.

### 6.2 The `RawJob` contract (`apps/sources/base.py`)

```python
@dataclass
class RawJob:
    source: str  # Source.name
    external_id: str  # stable ID within the source (slug, UUID, numeric id)
    url: str  # listing URL
    title: str
    company: str  # the real employer/platform, e.g. "micro1", even if found on an aggregator
    apply_url: str = ""
    description_html: str = ""
    description_text: str = ""
    pay_text: str = ""  # raw, e.g. "$50 - $80/hr" or "PPT Rate: $60 per task"
    locations: list[str] = field(
        default_factory=list
    )  # raw strings: "Global", "Kenya", "KE", "EMEA"…
    work_type: str = ""  # remote / onsite / hybrid / "" if unknown
    employment_type: str = ""
    posted_at: datetime | None = None
    raw: dict = field(default_factory=dict)  # original payload for re-parsing
```

### 6.3 `BaseSource` and two-phase fetching

```python
class BaseSource:
    kind: str  # matches Source.kind (or plugin name for custom)

    def __init__(self, source: Source, client: httpx.Client): ...
    def list_ids(self) -> list[str]: ...  # cheap: one request for the listing page
    def fetch_detail(self, external_id: str) -> RawJob: ...  # only called for unseen IDs
    def fetch_all_new(self, seen_ids: set[str]) -> Iterator[RawJob]: ...  # default orchestration
```

- **Phase 1 (list):** one request for the newest-first listing page, returning IDs.
- **Phase 2 (detail):** fetch detail pages only for IDs not already in the database for this source. Wait at least 1–2 seconds between requests to the same host.
- Sources whose listing already contains full data (APIs, RSS) may return `RawJob`s directly from the list call, with no detail calls needed.
- **HTTP client:**
  - one shared `httpx.Client` per run, with a 20s timeout and up to 2 retries with backoff on 5xx and timeouts;
  - User-Agent set from `SCRAPER_USER_AGENT` (default: a normal browser UA plus a contact URL);
  - respect HTTP 429 by backing off and marking the run degraded.

### 6.4 Source kinds and their `config`

The registry maps `kind` to a fetcher class via a `@register("<kind>")` decorator. `apps/sources/__init__.py` auto-imports every module in `generic/` and `custom/`, so adding a file is enough to register it.

| kind | Use for | `config` keys |
|---|---|---|
| `jsonld` | Sites whose detail pages embed schema.org `JobPosting` JSON-LD (e.g. **ExpertWoka**) | `list_url`, `link_selector` (CSS), `link_pattern` (regex for valid detail paths), `id_from` (`last_path_segment` or `regex`), `id_regex` (optional), `max_new_per_run` (default 30) |
| `ats` | Company boards on Greenhouse, Lever, Ashby, Workable | `ats` (`greenhouse | lever | ashby | workable`), `board` (company slug), `company` (display name). Each ATS adapter knows its public endpoint. **Verify the endpoint formats against current public docs when implementing.** |
| `json_api` | Any JSON endpoint (e.g. one found in browser DevTools) | `url`, `method` (GET), `params`, `items_path` (dotted path to the list), `map` (field → dotted path in each item), `id_field` |
| `rss` | RSS/Atom feeds | `url`, `company_from` (`feed_title | item_field | fixed`), `company` |
| `llm_extract` | Messy, low-volume sites with no structure | `list_url`, `link_selector`, `link_pattern`, `max_new_per_run` (default 10). Each detail page is stripped to text and the LLM returns `RawJob` fields (task `extract_page`, system-paid) |
| `custom` | Anything else | Plugin-specific; the plugin is looked up by `Source.name` |

### 6.5 First source: ExpertWoka (Phase 4)
ExpertWoka (`https://expertwoka.com`) is an aggregator of AI-training and remote roles. It lists jobs from micro1, Turing, TELUS Digital, Mercor, Alignerr, Mindrift, Appen, OneForma, Lionbridge and others, plus its own "ExpertWoka Sourcing" roles. What we know:
- Listing: `https://expertwoka.com/opportunities?sort=newest&page=N`, server-rendered HTML, 12 per page.
- Filters exist for category, company, location (including Kenya) and work type. **The exact query parameter names for the location filter are not yet known.** During Phase 4, inspect the filter form's field names in the live HTML and record the Kenya-filtered URL in the Source config. If it can't be determined, use the unfiltered newest list; our own eligibility logic (§7.3) filters anyway.
- Detail URLs look like `/opportunities/<slug>` (e.g. `/opportunities/photographer-micro1-2`) and `/opportunities/sourcing/<id>` for ExpertWoka's own roles. Exclude non-job links such as `/opportunities?…` and `/opportunities/` category pages.
- Detail pages embed **schema.org JobPosting JSON-LD**, observed to include `title`, `hiringOrganization.name`, `jobLocationType` (`TELECOMMUTE`), and `applicantLocationRequirements` (a list of `{name}` country entries). Inspect 3–5 detail pages to confirm which fields are reliably present (pay, date posted, description, apply URL) and map them. Save those pages as test fixtures.
- Some listings use referral or affiliate links. Keep the ExpertWoka link as `url`, and set `apply_url` to the direct platform link if the page exposes one.
- **Politeness:** interval 20 minutes, list page 1 only, at most 30 detail fetches per run, 1–2 seconds between requests. Check `https://expertwoka.com/terms` during Phase 4. If it forbids automated access, stop and tell the owner.

Seed this source with a data migration or a management command (`seed_sources`).

### 6.6 Running sources
- **Scheduler:** a Huey periodic task `sources.tasks.tick` runs every 5 minutes. For each enabled source whose `last_run_at + interval_minutes` has passed, it enqueues `run_source(source_id, trigger="schedule")`. A per-source lock (Huey's `lock_task` or a DB flag) prevents overlapping runs.
- **`run_source` service:**
  1. create a `SourceRun`;
  2. build the fetcher via the registry;
  3. collect `RawJob`s;
  4. pass them to `jobs.services.ingest_raw_jobs(...)`;
  5. record counts and errors;
  6. update source health.
- **Health:**
  - `consecutive_failures >= 3`, or zero IDs seen on 3 consecutive runs where IDs were previously seen, sets `failing`, and a `source_health` notification goes to staff users (Telegram if linked).
  - One failure sets `degraded`. A success resets to `ok`.
- **Manual runs:** an admin action "Run now" on Source, plus the user-facing "Search now" (§10.6).
- **Management command:** `python manage.py test_source <name> [--dry-run] [--limit N]`. It runs the fetcher and prints the parsed `RawJob`s as a table or JSON. With `--dry-run` it saves nothing. This is the standard way to verify a new source.
- **Documentation:** write `docs/SOURCES.md` explaining the decision tree for adding a site:
  1. Is JSON-LD present? Use `jsonld`.
  2. Is it on a known ATS? Use `ats`.
  3. Is there a JSON request in DevTools? Use `json_api`.
  4. Is there an RSS feed? Use `rss`.
  5. Is it low volume? Use `llm_extract`.
  6. Otherwise, write a `custom` plugin.

### 6.7 Testing sources
- Every fetcher has parser tests against **saved fixture files** in `tests/fixtures/html/<source>/`, with httpx mocked by respx.
- Generic fetchers are tested with small synthetic fixtures.
- Add a "site changed" test: given a fixture with no matching links, `run_source` records a degraded or failed run rather than crashing.

---

## 7. Ingestion: normalisation, deduplication, eligibility (`apps/jobs/services.py`)

`ingest_raw_jobs(source, raw_jobs) -> IngestResult` processes each `RawJob`:

### 7.1 Upsert
- Look up an existing Job by `(source, external_id)`. If it exists, update `last_seen_at` and any changed fields. Otherwise create it with `first_seen_at = now`.
- Sanitise `description_html` with nh3 (allow basic formatting tags and links). Derive `description_text`.
- Store `raw`.

### 7.2 Pay normalisation (`jobs/pay.py`)
Parse `pay_text` with regexes into `pay_min`, `pay_max`, `pay_currency`, `pay_period`. Handle at least these examples:
- `$50 - $80/hr`, `Up to $23/hr`, `$15 - $15/hr`, `Max rate: $20/hr`
- `$60 per task`, `PPT Rate: $60 per task; AHT: 6 hours` (period = `task`)
- `$2,535 one-time lump sum` (period = `one_time`)
- `$300000-500000` (annual when no period is given and the value is over 10,000)
- `KES 80,000 per month`, `Ksh 50k`, `USD 1,200/month`

Anything unparseable stays in `pay_text` only, with period `unknown`. This needs thorough unit tests.

### 7.3 Location and eligibility normalisation (`jobs/eligibility.py`)
- Map raw location strings to ISO-2 country codes (country names, common variants, codes) and region codes. The region table must define at least:
  - `GLOBAL`
  - `AFRICA` (all African countries)
  - `EMEA`
  - `EU`, `EUROPE`
  - `NA` (US, CA, MX)
  - `LATAM`
  - `APAC`
  - `MENA`
- Set `eligible_countries`, `eligible_regions`, and `is_global` ("Global", "Anywhere", "Worldwide", or the `GLOBAL` code).
- Set `eligibility_known = False` when nothing could be parsed.
- `is_eligible(job, country_code) -> bool | None`:
  - `True` if the job is global, the country is listed, or the country falls in a listed region;
  - `False` if the job lists countries or regions and none of them match;
  - `None` if eligibility is unknown.
- Unknown eligibility is **not** excluded. It is shown with an "eligibility unclear" badge.

### 7.4 Deduplication
- `dedupe_key = slug(normalised company) + ":" + slug(normalised title)`. Lowercase it, strip punctuation, remove seniority noise words only if needed, and collapse whitespace.
- When a new job is created, look for an **active** job with the same `dedupe_key` from a **different** source, first seen within the last 30 days. If found:
  - set `duplicate_of` on the newer job;
  - add the newer job's URL to the canonical job's `alternate_urls`;
  - if the newer job comes from the platform itself (its source company matches the job company) and the canonical came from an aggregator, prefer the platform URL as the canonical `apply_url`.
- Duplicates are **never matched or notified** to users. Only canonical jobs are.

### 7.5 Expiry
- A Huey task runs daily and marks a job `expired` if it wasn't seen in its source's listing for N days (default 7), or its detail page returns 404 or 410 when re-checked.
- Manual and email jobs expire after 45 days unless re-seen.
- Expired jobs drop out of alerts and are de-emphasised in the UI.

### 7.6 After ingestion
Enqueue `jobs.tasks.extract_job(job_id)`. This is the LLM structural extraction (§11.6), done **once per job** and paid by the system. Then enqueue `matching.tasks.match_new_job(job_id)`, which runs the rule filter for all active users (§10).

### 7.7 Scam signals (`jobs/scam.py`)
Simple heuristics add entries to `scam_flags`:
- the description asks for payment, a "registration fee" or "training fee";
- it asks for ID or bank details before an interview;
- it uses a messaging app as the only contact (Telegram or WhatsApp handles) for "AI training" roles;
- pay is far above the norm for the stated work.

Flagged jobs show a warning badge. They are never hidden automatically.

---

## 8. Manual job input (`apps/jobs`)

### 8.1 Paste a URL
- Form: URL field. The service `add_job_from_url(user, url)` works through these steps:
  1. **Known source:** if the URL matches a configured source's detail pattern (e.g. an ExpertWoka opportunity URL), use that fetcher's `fetch_detail` and ingest the job as `origin=scraped`, `visibility=public`.
  2. **JSON-LD present:** if the page has JobPosting JSON-LD, parse it as `origin=manual_url`, `visibility=public`.
  3. **Otherwise:** strip the page to text and call LLM task `extract_page` (counts toward the user's `monthly_manual_extractions`).
  4. **Blocked domains:** LinkedIn job pages and other sites that block scraping are not fetched. Show the message "This site can't be read automatically. Paste the job description instead."
- Validate the URL: http/https only, no private or localhost IPs (SSRF protection), response size capped at 2 MB, timeout 20s.
- After the job is created or found, create a Match for the user, score it immediately (counting toward quota), and redirect to the job detail page.

### 8.2 Paste a job description
- Form: large textarea, plus optional company, title and URL fields.
- `add_job_from_text(user, text, hints)` calls LLM task `extract_job` on the text and creates a Job with `origin=manual_text`, `visibility=private`, `owner=user`.
- It is then matched and scored like any other job.

### 8.3 Shared rules
- Rate limit: 20 manual additions per user per day.
- Pasted text is **untrusted input** (§11.5).

---

## 9. Email job-alert ingestion (`apps/inbound`)

### 9.1 Design: forwarding addresses, not mailbox access
We never store users' email passwords or OAuth tokens. Instead:

1. The owner's domain is on Cloudflare (free DNS), with **Cloudflare Email Routing** configured as a catch-all for `INBOUND_EMAIL_DOMAIN` (e.g. `alerts.example.com`) that forwards everything to **one dedicated Gmail inbox** owned by the platform.
2. Each user gets a unique `InboundAddress` such as `u-7f3k9a@alerts.example.com`, shown on their settings page with step-by-step instructions for creating a Gmail filter (and an Outlook rule) that forwards LinkedIn and other job-alert emails to it.
3. A Huey periodic task polls the dedicated inbox over **IMAP** (`imap-tools`) every 5 minutes. Credentials come from env (`INBOUND_IMAP_HOST`, `INBOUND_IMAP_USER`, `INBOUND_IMAP_PASSWORD`; a Gmail app password).

### 9.2 Processing each message
1. **Find the recipient token** from the `To`, `Delivered-To`, `X-Forwarded-To` or `X-Original-To` headers. Unknown or no token: mark ignored and move to a `jobpilot/unrouted` label or folder.
2. **Deduplicate** by `Message-ID`.
3. **Gmail forwarding confirmation:** if the email is Gmail's forwarding-confirmation message (sender like `forwarding-noreply@google.com`, subject containing "Forwarding Confirmation"), extract the confirmation link and code into `ForwardingConfirmation`. The user sees a banner on their dashboard: "Click to confirm forwarding". Also notify them on Telegram if linked. Without this the user can't finish setup.
4. **Pick a parser** by sender or domain (a parser registry like the sources one):
   - `LinkedInAlertParser`: LinkedIn job-alert emails. Extract each job's title, company, location and the `/jobs/view/<id>` link **from the email body itself**. **Do not fetch LinkedIn pages.** Create Jobs with `origin=email`, `external_id = linkedin:<id>`, `url` = the LinkedIn link, `visibility=public`.
   - `GenericAlertParser`: for any other sender. Collect links from the body. Links matching a configured source's detail pattern are fetched through that source. Otherwise the LLM extracts jobs from the email text (task `extract_email_jobs`, system-paid, capped per email).
   - Write parsers for BrighterMonday, Fuzu and MyJobMag when sample emails are available. Until then the generic parser handles them.
5. **Ingest** through `jobs.services.ingest_raw_jobs` (dedupe applies). Then match the found jobs for **the receiving user first**: score and notify them as usual. Public email-sourced jobs also flow to other users' rule filters.
6. **Mark the message** read and move it to `jobpilot/processed`.

### 9.3 Retention and safety
- `InboundEmail.body_html` and `body_text` are cleared after 30 days by the retention task. Metadata stays.
- Email content is untrusted (§11.5). Sanitise anything displayed.

### 9.4 Testing
Use saved `.eml` fixtures in `tests/fixtures/emails/`: LinkedIn alert, generic alert, Gmail confirmation, unrouted. Mock IMAP behind an interface (`MailboxClient` protocol with a fake implementation).

---

## 10. Matching: rule filter + LLM scoring (`apps/matching`)

### 10.1 When matching runs
- **New canonical job:** `match_new_job(job_id)` runs the rule filter for every active user with a completed profile. It creates a `Match` only when `rule_pass` is true. Failed jobs aren't stored, to keep the table small; store them only in debug mode.
- **User changes profile or track:** `rematch_user(user_id)` re-runs the rule filter over active jobs from the last 14 days, and marks existing scores stale (`scored_profile_version != profile.version`). Stale scores are refreshed lazily when viewed, or when the user clicks "Rescore". Don't rescore everything automatically, because of cost.
- **Manual or email job:** matched immediately for that user.

### 10.2 Rule filter (`matching/rules.py`, deterministic, no LLM)
For each (user, job), evaluate against each of the user's tracks and pick the track with the best keyword overlap. The job passes when **all** of these hold:
1. **Eligible:** `is_eligible(job, user.settings.country)` is not `False`. Unknown passes, with a flag.
2. **Work type** is in `track.work_types`. Unknown passes. For onsite or hybrid jobs, the location must match `track.locations`.
3. **Pay:** if `track.min_pay_amount` is set and the job's pay is parsed **in a comparable period and currency**, `pay_max` (or `pay_min`) must be at least the minimum. Unknown or incomparable passes. Keep currency conversion out of scope for now: compare only matching currencies, otherwise pass with a flag.
4. **Not excluded:** the company isn't in `excluded_companies`, and no `exclude_keywords` appear in the title.
5. **Relevant:** at least one of
   - a `target_titles` / `include_keywords` term appears in the title or description, or
   - at least 2 of the user's skill names appear in the description or extracted required skills.

Record the human-readable reasons in `rule_reasons`, e.g. `"eligible: global"`, `"keyword: django"`, `"pay unknown"`.

### 10.3 LLM scoring (`matching/scoring.py`)
- Runs for Matches with `rule_pass=True`, as part of matching for new jobs, **subject to quota** (`monthly_scores`). When the quota is exhausted, matches stay unscored with the badge "Score limit reached", and the user is told once per month.
- Model: `LLM_MODEL_FAST`. Prompt `score_match.v1`.
- Input:
  - a **compact profile summary**: a deterministic text built from Profile + the selected Track + top skills + recent experience titles, cached per `profile.version`;
  - the job's `extracted` JSON (or title + description text if not yet extracted).
- Output schema:

```python
class MatchScore(BaseModel):
    score: int  # 0-100
    verdict: Literal["strong", "good", "weak", "poor"]
    strengths: list[str]  # max 4, each <= 15 words, grounded in the profile
    gaps: list[str]  # max 4, each <= 15 words
    summary: str  # <= 25 words, plain language
```

- **Scoring rubric** (in the prompt):
  - required skills overlap (40%);
  - relevant experience (25%);
  - seniority fit (15%);
  - track and title alignment (10%);
  - practical fit: work type, pay, location (10%).
  - **Never** reward skills the profile doesn't contain.

### 10.4 After scoring
- If `score >= settings.instant_score_threshold` and `notify_mode == instant`, enqueue an instant notification (§13).
- If `score >= settings.auto_kit_threshold`, the kit quota allows it, and no kit exists yet, enqueue kit generation *before* the instant notification is sent. The notification is delayed up to 90 seconds waiting for the kit, then sent with or without it.

### 10.5 Dashboard job feed
- Default view: active, rule-passed matches, sorted by score (unscored last), then recency.
- Filters: track, verdict, status, work type, source/company, eligibility (eligible only / include unclear), "new since last visit".
- Each card shows: title, company (and platform), pay, eligibility badge, work type, score + verdict, a one-line summary, age ("2h ago"), and scam or expired badges.
- Actions:
  - **View**: job detail with full description, strengths/gaps, and the kit panel;
  - **Save**;
  - **Dismiss**, with a reason (feeds future tuning);
  - **Generate kit**;
  - **Mark applied**, which creates an Application.

### 10.6 "Search now"
A dashboard button that:
1. enqueues `run_source` for all enabled sources whose last run is more than 5 minutes old;
2. then rescores this user's stale and unscored matches from the last 7 days, within quota.

Rate-limited to once per 10 minutes per user (`UserSettings.last_manual_search_at`). Show progress via HTMX polling ("Checking 3 sources…", "4 new matches").

Staff users also see per-source "Run now" buttons on an admin status page.

---

## 11. LLM layer (`apps/llm`)

### 11.1 Client API

```python
def call_structured(
    *, task: str, prompt_name: str, variables: dict, schema: type[BaseModel],
    user: User | None, model_tier: Literal["fast", "writer"] = "fast",
    max_output_tokens: int = 1500, cache_key: str | None = None,
) -> BaseModel: ...

def call_text(... same, returns str ...)  # for cover letters if not using a schema
```

Responsibilities:
1. **Quota check** before the call: per-user monthly counters by task group, plus the monthly USD cap. Raise `QuotaExceeded`. System calls (`user=None`) aren't capped per user, but have a global daily cap `LLM_SYSTEM_DAILY_USD_CAP`.
2. **Render the prompt** from `apps/llm/prompts/<prompt_name>.md`. Each prompt file starts with front-matter (`version`, `task`, `tier`) and uses simple `{{ variable }}` substitution. Prompt versions are logged.
3. **Call the provider.**
4. **Validate the output** against the pydantic schema. On a validation error, retry **once** with a repair message that includes the validation error. If that fails, raise `LLMOutputError`.
5. **Log** an `LLMCall` with tokens, cost (from the price table) and latency.
6. **Cache:** if `cache_key` is given, return a previously stored result for the same key and prompt version (stored in an `LLMCache` table: key, prompt_version, result JSON, created_at). Use this for job extraction and for profile summaries.

### 11.2 Providers
- `providers/base.py`: `class LLMProvider(Protocol): def complete(self, *, model, system, messages, max_tokens, json_mode) -> ProviderResponse` (text, input_tokens, output_tokens).
- `providers/openai_compat.py`: the Chat Completions API over httpx, configured with `LLM_BASE_URL`, `LLM_API_KEY`. Use JSON mode or `response_format` when the provider supports it; otherwise instruct JSON-only output and extract the first JSON object.
- `providers/fake.py`: deterministic responses loaded from `tests/fixtures/llm/<task>.json`, falling back to schema-valid defaults. **Used in all tests** and selectable in dev via `LLM_PROVIDER=fake`, so the app runs without an API key.

### 11.3 Model tiers and prices
- Env: `LLM_PROVIDER` (`openai_compat | fake`), `LLM_MODEL_FAST`, `LLM_MODEL_WRITER`.
- Env `LLM_PRICES_JSON` maps model to `{"input_per_mtok": x, "output_per_mtok": y}`. **Don't hardcode prices.** If a model is missing, log cost 0 with a warning.

### 11.4 Quota defaults (settings, overridable per user in the admin)
- `monthly_scores`: 600
- `monthly_kits`: 40
- `monthly_manual_extractions`: 60
- `monthly_cost_usd_cap`: 3.00

The owner can raise any of these per user. The dashboard shows usage in Settings → Usage.

### 11.5 Prompt-injection and untrusted input rules
Job descriptions, scraped pages, pasted text and emails are **untrusted**:
- Put untrusted content inside clearly delimited blocks (`<job_posting>…</job_posting>`), with an instruction that it is data and that any instructions inside it must be ignored.
- The LLM only returns **data validated by schemas**. It never chooses actions, URLs to fetch, or recipients.
- Never render LLM output as raw HTML. Escape it, or sanitise it with nh3.
- Cap untrusted input length: truncate job text to about 12k characters and email text to about 20k.

### 11.6 Job extraction (task `extract_job`, system-paid, once per job)

```python
class JobExtraction(BaseModel):
    summary: str  # <= 40 words
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "unknown"]
    required_skills: list[str]  # max 15, normalised names
    nice_to_have_skills: list[str]  # max 10
    responsibilities: list[str]  # max 8, short
    requirements: list[str]  # max 8, short (degrees, years, languages, hours/week…)
    hours_commitment: str | None
    pay_text: str | None  # if found in text and source lacked it
    locations: list[str]  # if found in text and source lacked it
    work_type: Literal["remote", "onsite", "hybrid", "unknown"]
    application_steps: list[str]  # e.g. "AI interview", "coding test", "ID verification"
    red_flags: list[str]  # scam-like signals, empty if none
```

Fill missing Job fields (pay, locations, work_type) from the extraction when the source didn't provide them, then re-run the eligibility and pay normalisation.

### 11.7 CV parsing (task `parse_cv`)
Covered in §15.4.

---

## 12. Application kit generation (`apps/kits`)

### 12.1 Trigger
- The user clicks "Generate kit" on a job, or the system auto-generates for high scores (§10.4).
- Counts toward `monthly_kits`.
- Runs as a Huey task. The UI polls every 2 seconds via HTMX with status text ("Selecting your most relevant experience…", "Writing cover letter…", "Rendering documents…").

### 12.2 Pipeline (`kits/services.py: generate_kit(user, job, instruction=None)`)
1. **Build the context:**
   - the job's `extracted` data, title, company, and description (truncated);
   - the selected Track and its `cv_emphasis`;
   - the **full profile serialised with IDs**: `EXP-12`, `BUL-88`, `PRJ-3`, `SKL-40`, `EDU-2`, `CERT-5`.
2. **CV:** writer model, prompt `generate_cv.v1`, schema in §12.3.
3. **Validate** (§12.5). If it fails, retry once with the validation errors fed back. If it still fails, drop the failing bullets and record them in `validation_report`.
4. **Cover letter:** writer model, prompt `generate_cover_letter.v1`, given the validated CV data and the job.
5. **Interview prep:** fast model, prompt `generate_interview.v1`.
6. **Screening answers:** fast model, prompt `generate_screening.v1`. Short answers to common application and AI-screening questions: "Why this role?", "Describe a relevant project", "Availability / hours per week", "Expected rate", "Tell me about yourself (60–90 seconds)".
7. **Render** the PDF and DOCX for the CV and the cover letter (§12.6) and save them to private storage.
8. **Finish:** set status `ready` and notify if requested (`kit_ready`).

### 12.3 CV data schema

```python
class CVBullet(BaseModel):
    text: str  # <= 30 words
    source_ids: list[str]  # e.g. ["BUL-88"] or ["EXP-12","SKL-40"] — must be non-empty


class CVExperience(BaseModel):
    experience_id: str  # "EXP-12"
    bullets: list[CVBullet]  # 2–5


class CVProject(BaseModel):
    project_id: str
    bullets: list[CVBullet]  # 1–3


class CVData(BaseModel):
    headline: str  # e.g. "IT Support Engineer | Windows Server, Active Directory, Networking"
    summary: str  # 2–3 sentences, <= 60 words
    summary_source_ids: list[str]
    skills: list[str]  # skill IDs, ordered by relevance to the job, max 14
    experiences: list[CVExperience]  # ordered; may omit irrelevant ones
    projects: list[CVProject]  # 0–3
    education_ids: list[str]
    certification_ids: list[str]
```

Factual fields (company names, dates, titles, institutions, contact details) are **never generated by the LLM**. They are filled from the database by ID at render time. The LLM only chooses, orders, and rephrases.

### 12.4 Writing style rules (put these in every writing prompt)
The owner wants documents that read as if a capable person wrote them, not an AI:
- Plain, specific, confident language. Short sentences mixed with a few longer ones.
- Start bullets with concrete verbs ("Set up", "Fixed", "Built", "Migrated", "Supported", "Wrote"). Don't overuse "Led" and "Managed".
- Use numbers **only** when they exist in the referenced profile items.
- **Banned words and phrases** (validator rejects them; keep the list in `kits/style.py`): "leverage", "spearheaded", "synergy", "dynamic", "passionate", "results-driven", "proven track record", "go-getter", "cutting-edge", "seamless(ly)", "robust", "detail-oriented professional", "I am excited to apply", "I am writing to express my interest", "thrilled", "delve", "tapestry", "in today's fast-paced", "hit the ground running", "team player", "think outside the box", "utilize".
- **No em dashes** in generated text. Use commas, full stops or parentheses.
- Cover letter: 180–300 words, 3–4 short paragraphs:
  1. which role and one genuine reason it fits;
  2. one or two specific matching experiences that tie to stated requirements;
  3. practical fit (availability, remote setup, timezone overlap if relevant);
  4. a short close.
  No flattery of the company.
- Match the job's wording for key skills where the profile genuinely has them (ATS keyword alignment), but never stuff keywords.
- British or American spelling follows the job posting. Default to British (Kenyan convention).

### 12.5 Fabrication and quality validator (`kits/validation.py`)
Checks performed on `CVData`:
1. Every referenced ID exists and belongs to this user.
2. Every bullet has at least one `source_id`.
3. **Number check:** every number or percentage in a bullet or the summary must appear in the text or metrics of one of its referenced source items. Normalise "40%" and "40 percent".
4. **Skill check:** skills named in bullets must exist in the user's Skill list or in the referenced items' text (case-insensitive, simple stemming).
5. No banned phrases (§12.4). No em dashes.
6. Length limits respected.

The result goes in `validation_report` as `{passed: bool, issues: [{field, message}]}`. The kit UI shows a small "All claims trace to your profile ✓" indicator, or lists any issues.

### 12.6 Rendering
- **CV PDF:** WeasyPrint from `kits/templates_docs/cv_classic.html` + CSS. It must be **ATS-friendly**: single column, standard headings (Summary, Skills, Experience, Projects, Education, Certifications), real selectable text, no tables for layout, no images or icons, A4, 1–2 pages, a clean professional font (bundle a free font such as Inter or Source Sans 3 in the image).
- **CV DOCX:** docxtpl from `kits/templates_docs/cv_classic.docx` with the same structure.
- **Cover letter:** `cover_letter.html` (PDF) and `cover_letter.docx`.
- **File names:** `<First>_<Last>_CV_<Company>.pdf` (sanitised, ASCII).
- **Re-rendering:** if the user edits the CV text or cover letter in the UI (simple per-bullet textareas and a cover-letter textarea), set `user_edited=True` and re-render the files without calling the LLM. Edited text still goes through the banned-phrase check, but only shows warnings and never blocks.

### 12.7 Kit UI (on the job detail page)
- Tabs or sections: CV preview (HTML), cover letter, interview prep, screening answers.
- Download buttons for CV and cover letter, each as PDF or DOCX, served by `kits.views.download` after an ownership check.
- **Regenerate with instruction:** a text field ("shorter", "more technical", "emphasise IT support"). This creates a new Kit version, and old versions stay available in a dropdown.
- **Copy buttons** for the cover letter and screening answers (for pasting into application forms).

### 12.8 Interview prep schema

```python
class InterviewItem(BaseModel):
    category: Literal["technical", "behavioural", "role", "company", "gap"]
    question: str
    why_they_ask: str  # <= 20 words
    answer_outline: list[str]  # 2–4 bullet points drawn from the profile
    source_ids: list[str]
```

Produce 10–15 items. Include at least 2 `gap` questions that address the match's gaps honestly (how to talk about missing skills).

---

## 13. Notifications (`apps/notifications`)

### 13.1 Channels
- **Telegram** (python-telegram-bot, long polling, `manage.py run_bot`).
  - **Linking:** the settings page has a "Connect Telegram" button that creates a `TelegramLinkToken` (valid 15 minutes) and opens `https://t.me/<BOT_USERNAME>?start=<token>`. The bot's `/start <token>` handler validates the token, creates or updates the `TelegramLink`, and replies "Connected to JobPilot as <email>". Unknown chats get a polite "This bot is invite-only" message.
  - **Commands:**
    - `/status`: new matches today, applications by stage;
    - `/search`: same as "Search now", rate-limited;
    - `/digest`: send a digest now;
    - `/pause` and `/resume`: toggle `notify_mode` between off and the previous mode;
    - `/help`.
  - **Inline buttons** on match messages:
    - `Open`: URL button to the job page;
    - `Kit`: generate the kit, or send its download links if ready;
    - `Applied ✓`: creates an Application in stage `applied`;
    - `Not interested`: dismisses the match.
  - Callback data carries a signed, short ID (`django.core.signing` or an HMAC). Every callback verifies that the chat belongs to the match's user.
  - **Kit delivery on Telegram:** send the CV PDF and cover letter PDF as documents when ready, if the user enables "Send documents on Telegram" (a UserSettings flag, default on).
- **Email** (Django SMTP backend): HTML + plain-text templates, sent from `DEFAULT_FROM_EMAIL` on the owner's domain. Used for digests, kit-ready messages (links only, no attachments), follow-up reminders, invites and auth emails.

### 13.2 Modes (`UserSettings.notify_mode`)
- `instant`: matches at or above `instant_score_threshold` are sent as soon as they're scored. Everything else goes into a **daily** digest.
- `hourly`: one bundled message per hour if there are new matches.
- `daily`: one digest at `digest_time` in the user's timezone.
- `off`: nothing is sent, but the dashboard still updates.

**Quiet hours:** instant and hourly messages that fall inside quiet hours are deferred until quiet hours end.

### 13.3 Dispatcher
A Huey periodic task, `notifications.tasks.dispatch`, runs every 5 minutes:
- For each user due for an hourly or daily digest (based on `last_digest_sent_at`, mode, `digest_time` and timezone), it collects matches with status `new`, rule-passed, and either score ≥ 40 or unscored. The digest includes up to 15, sorted by score, plus "and N more on your dashboard".
- After sending, it sets those matches to `notified` and updates `last_digest_sent_at`.

Instant notifications are enqueued directly by scoring (§10.4) and go through the same `send_notification` service. That service checks `dedupe_key`, so retries never double-send.

### 13.4 Message formats
**Instant (Telegram):**

```
🟢 92 · Senior Software Engineer — micro1
$40–60/hr · Remote · Global (eligible: Kenya)
Strong: Django, Docker, CI/CD · Gap: Kubernetes
Posted 25 min ago via ExpertWoka
[Open] [Kit] [Applied ✓] [Not interested]
```

**Digest (Telegram and email):** a header with the count, then one line per match (score, title, company, pay), with links. The email version uses a clean branded HTML template.

### 13.5 Follow-up reminders
A daily task checks Applications in stage `applied` with `next_followup_at` in the past. It sends one reminder per application and then sets the next follow-up 7 days later. After 21 days with no stage change, it suggests marking the application `ghosted`.

---

## 14. Application tracker (`apps/tracker`)

- **Creating applications:**
  - "Mark applied" from a match, from Telegram, or added manually for jobs found outside the system. A manual entry needs a title, company, an optional URL, and a date.
  - Creating one sets the match status to `applied`, `applied_at`, `next_followup_at = applied_at + 7 days`, and links the kit used (the latest ready kit for that job, editable).
- **Kanban board** (`/tracker/`): one column per stage, drag and drop with SortableJS, and an HTMX POST to `tracker.services.change_stage(application, to_stage, note)`. That service creates an `ApplicationEvent` and updates timestamps. On mobile, show a stage dropdown per card instead of drag and drop.
- **Application detail:** job info, the kit version used (with download links), notes, the event timeline, and interviews (add, edit).
- **Personal stats** (`/tracker/stats`):
  - counts by stage;
  - response rate (anything past `applied`, divided by applied);
  - response rate by source/company platform, by track, and by kit vs no kit;
  - median days to first response.
  Simple tables, plus CSS bars or Chart.js from cdnjs.
- **Outcome data for future insights:** no extra UI now. Make sure `ApplicationEvent`, `Kit`, `Match`, `Job.source` and `Track` links are always populated, so the backlog insights feature has clean history.

---

## 15. Accounts, invites and onboarding (`apps/accounts`, `apps/profiles`, `apps/pages`)

### 15.1 Auth (django-allauth)
- Login by email (no username). Email verification is **mandatory**. Password reset by email. Google sign-in through allauth's Google provider, with client ID and secret from env.
- Use the settings for the installed allauth version. For recent versions this means `ACCOUNT_LOGIN_METHODS = {"email"}`, `ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]`, `ACCOUNT_EMAIL_VERIFICATION = "mandatory"`, `SOCIALACCOUNT_EMAIL_AUTHENTICATION = True`. **Check the installed version's documentation; don't rely on outdated setting names.**
- Style the allauth templates to match the app.
- Optional MFA (allauth.mfa) is backlog.

### 15.2 Invite-only signup
- `/join/<code>/` validates the invite (exists, not used, not expired, and the email matches if the invite is locked to one), stores the code in the session, and shows the signup page with "Continue with Google" and an email form.
- `InviteOnlyAccountAdapter.is_open_for_signup` and `InviteOnlySocialAccountAdapter.is_open_for_signup` return True only when the session holds a valid invite code. **Existing users can always log in**, including with Google if their email matches.
- When signup completes (allauth `user_signed_up` signal):
  - mark the invite used;
  - create `UserSettings`, `Profile`, `UsageQuota` and `InboundAddress`;
  - record `ConsentRecord`s for terms and privacy (the signup form has a required checkbox).
- **Admin:**
  - generate invites (single or bulk), with a copy-link action;
  - send an invite by email;
  - list waitlist entries, with an "Approve → create invite and email it" action.
- **Public waitlist form** on the landing page ("Request an invite"): name, email and reason, rate-limited, saved as `WaitlistEntry`. The owner is notified on Telegram.

### 15.3 Onboarding wizard (shown until `onboarding_completed`)
1. **Upload your CV** (PDF or DOCX, max 5 MB), or skip and fill in manually.
2. **Review your profile:** the parsed data is shown in editable forms, then the user confirms.
3. **Choose your target:** create the first Track (titles, keywords, work types, minimum pay, employment types).
4. **Location and timezone:** country (default KE) and timezone (default Africa/Nairobi).
5. **Notifications:** choose the mode and digest time, connect Telegram (optional), and toggle email.
6. **Job-alert forwarding (optional):** show the user's forwarding address and instructions. Can be done later.
7. **Done:** run the first matching pass and send the user to the dashboard, with a "first matches loading…" state.

### 15.4 CV import (`profiles/services.py: import_cv`)
1. Extract the text: pypdf for PDF, python-docx for DOCX. Reject scanned PDFs that contain no text, with a friendly message (OCR is backlog).
2. LLM task `parse_cv` (writer or fast tier; use fast if quality is acceptable) with a schema mirroring the profile models: headline, summary, contact, experiences with bullets and metrics, projects, education, certifications, skills with category and level guesses.
3. Store the result in `CVImport.parsed_data` and show the review screen.
4. On confirm, create the profile objects in a transaction and bump `profile.version`.
5. Re-importing offers "replace" or "merge (add new items only)".

**Rule for the parse prompt:** extract only what the CV states. Never infer metrics or skills that aren't written.

### 15.5 Profile editor
- `/profile/` lists all sections with HTMX inline add, edit, delete and reorder.
- Each experience shows its bullets, and each bullet has an optional metrics field with the hint "Put the real numbers here, e.g. 'handled ~30 tickets/day'. The CV generator may only use numbers you enter."
- A **profile completeness** meter with tips: add metrics, add at least 8 skills, describe projects.
- **Tracks** are managed at `/profile/tracks/`.

### 15.6 Settings pages
- **Notifications:** mode, digest time, quiet hours, thresholds, channels, Telegram connect/disconnect.
- **Job alerts:** forwarding address, instructions, recent inbound emails with their status, and the confirmation banner.
- **Usage:** this month's LLM usage against quota.
- **Privacy:** export data, delete account, consent settings.
- **Account:** email, password, connected Google account (allauth pages).

---

## 16. Multi-user isolation

- **Every per-user model** inherits `UserOwnedModel`. Views fetch objects with `Model.objects.for_user(request.user)` or `get_object_or_404(Model.objects.for_user(request.user), pk=...)`. Use a mixin `OwnedObjectMixin` for class-based views.
- **Jobs:** users can see public jobs, plus private jobs only when `owner == user`. Provide `Job.objects.visible_to(user)`.
- **Files:** kits and CV uploads are stored **outside** the public media root (`PRIVATE_MEDIA_ROOT`). They are served only through views that check ownership, returning a `FileResponse` with `Content-Disposition: attachment`. Never put private files under `MEDIA_URL`.
- **Telegram callbacks** verify that the chat belongs to the object's user.
- **Huey tasks** receive IDs and re-check ownership inside services.
- **Admin:** only staff can see cross-user data. Staff is the owner only, by default.
- **Isolation tests (required):** a parametrised test suite creates users A and B and checks that A gets 404 for every detail, edit, download and action URL belonging to B. That includes matches, jobs (private), kits, kit files, applications, interviews, profile items, CV imports, inbound emails and Telegram callbacks. Extend this suite in every phase that adds per-user objects.

---

## 17. Frontend and UI

### 17.1 Principles
- Server-rendered Django templates. HTMX for partial updates. Alpine.js for small client-side state such as dropdowns, modals and tabs. **No SPA framework, no Node build.**
- **Tailwind CSS through the standalone CLI binary.** `scripts/setup_tailwind.sh` downloads the right binary for the OS and architecture (including linux-arm64 for the Oracle VM) into `bin/`. The Docker build runs it to produce `static/css/app.css`.
- Vendor and pin `htmx.min.js`, `alpine.min.js` and `Sortable.min.js` in `static/js/`. No CDN dependence for core JS.
- **Mobile-first and responsive.** Test layouts at 360px width. A bottom nav on mobile (Jobs, Tracker, Add, Profile, Settings); a sidebar or top nav on desktop.
- **Professional look:**
  - a neutral palette with one brand accent colour;
  - one font family (Inter or similar, self-hosted);
  - consistent spacing and cards with subtle borders;
  - clear empty states ("No matches yet. We check for new jobs every 20 minutes.");
  - loading states for every async action;
  - toast messages via Django messages and HTMX.
- **Accessibility:** semantic HTML, labels on all inputs, sufficient contrast, focus states, keyboard-usable kanban fallback (the stage dropdown).

### 17.2 Pages
| Area | URL | Phase |
|---|---|---|
| Landing (public): product pitch, how it works, "Request an invite" | `/` | 1 |
| Privacy policy, Terms of use (public) | `/privacy/`, `/terms/` | 1 |
| Join via invite | `/join/<code>/` | 1 |
| Auth pages (allauth, styled) | `/accounts/...` | 1 |
| Onboarding wizard | `/welcome/...` | 3 (finished in later phases) |
| Dashboard / job feed | `/app/` | 6 |
| Job detail + kit panel | `/app/jobs/<id>/` | 6/7 |
| Add job (URL / paste text) | `/app/jobs/add/` | 5 |
| Profile editor, tracks, CV import | `/app/profile/...` | 3 |
| Tracker board, application detail, stats | `/app/tracker/...` | 9 |
| Settings (notifications, job alerts, usage, privacy, account) | `/app/settings/...` | 1+ |
| Staff status page (sources health, runs, LLM spend) | `/app/staff/status/` | 4+ |
| Health check | `/healthz` | 0 |
| Custom 404 / 500 | — | 1 |

The landing page and legal pages must read like a real product. The privacy policy has to accurately describe:
- what data is stored (profile, CV, applications, forwarded alert emails);
- that LLM providers process the data to generate documents;
- retention periods;
- how to export or delete data;
- the contact email.

Use placeholders for the company and legal name that the owner fills in.

---

## 18. Background tasks and schedules (Huey)

Settings: `HUEY = {"huey_class": "huey.SqliteHuey", "filename": HUEY_DB_PATH, "immediate": <True in tests>, "results": False, "consumer": {"workers": 4, "worker_type": "thread"}}`.

| Task | Schedule / trigger | Phase |
|---|---|---|
| `sources.tick` → `run_source(id)` | every 5 min (each source on its own interval) | 4 |
| `jobs.extract_job(id)` | after ingestion | 4 |
| `matching.match_new_job(id)` | after extraction (or ingestion if extraction fails) | 6 |
| `matching.rematch_user(user_id)` | on profile or track change | 6 |
| `matching.search_now(user_id)` | user action | 6 |
| `kits.generate_kit(kit_id)` | user action / auto | 7 |
| `notifications.dispatch` | every 5 min | 8 |
| `notifications.send_instant(match_id)` | after scoring | 8 |
| `tracker.followup_reminders` | daily 07:00 UTC | 9 |
| `inbound.poll_mailbox` | every 5 min | 10 |
| `jobs.expire_stale` | daily 02:00 UTC | 4 |
| `core.retention_cleanup` | daily 03:00 UTC | 11 |
| `core.backup_db` | daily 01:30 UTC (prod only) | 12 |
| `core.heartbeat` | every 10 min (pings Healthchecks URL if set) | 12 |

Rules:
- Every task is wrapped with `@logged_task` (§21.1).
- Tasks are thin and call services.
- Tasks are **idempotent** (safe to run twice).
- Periodic tasks use Huey's `lock_task` to avoid overlapping.
- Every periodic task logs its start, end and counts.

---

## 19. Testing strategy

- **Test settings** (`settings/test.py`):
  - SQLite in-memory;
  - `HUEY["immediate"] = True`;
  - `LLM_PROVIDER = "fake"`;
  - locmem email backend;
  - fast password hasher;
  - private media in a temp directory.
- **Network blocked in tests:** an autouse fixture patches `socket.socket.connect` to raise for non-local hosts, or use `pytest-socket` if preferred (justify it). External HTTP is mocked with **respx**. IMAP and Telegram are behind interfaces with fakes.
- **factory-boy** factories for every model in `tests/factories.py`.
- **Required test categories per phase:**
  - unit tests for pure logic: pay parsing, eligibility, dedupe keys, rule filter, validator, banned phrases, digest due-time calculations;
  - service tests (DB);
  - view tests for status codes, permissions and HTMX partials;
  - the **isolation suite** (§16);
  - fixture-based parser tests for sources and email parsers.
- **LLM output quality:** keep `tests/fixtures/llm/` responses realistic. Validator tests include deliberately fabricated outputs (invented numbers, unknown IDs, banned phrases) that must be caught.
- **Time-dependent logic** (digests, quiet hours, follow-ups, expiry) is tested with **time-machine** across timezones, including Africa/Nairobi.
- **Coverage:** aim for 85%+ on `services.py`, `rules.py`, `eligibility.py`, `pay.py` and `validation.py`. Don't chase coverage on templates.

---

## 20. Security, privacy and compliance

- **Django production settings:**
  - `DEBUG=False`, `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`;
  - `SECURE_HSTS_SECONDS` (start at 3600, raise later), `SECURE_PROXY_SSL_HEADER` (behind Caddy), `X_FRAME_OPTIONS=DENY`;
  - `SECURE_CONTENT_TYPE_NOSNIFF`, `SESSION_COOKIE_HTTPONLY`;
  - `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` from env.
- **CSP** via django-csp: self-hosted scripts only (HTMX and Alpine are vendored). If Alpine needs `unsafe-eval`, use the Alpine CSP build.
- **Rate limits (django-ratelimit):**
  - login and signup: 10/min/IP;
  - waitlist: 5/hour/IP;
  - manual job add: 20/day/user;
  - search now: 1/10min/user;
  - kit generation: 10/hour/user;
  - Telegram commands: 30/min/chat.
- **Uploads:** PDF and DOCX only, checked by both extension and magic bytes, max 5 MB, private storage, random file names.
- **SSRF protection** for paste-URL: http/https only, resolve DNS and reject private, loopback and link-local addresses, no redirects to private addresses, size and time caps.
- **Sanitisation:** nh3 for any third-party HTML shown. Django auto-escaping everywhere else. Never use `|safe` on untrusted content.
- **Secrets** only via env. `.env` is git-ignored. Rotate the IMAP app password and API keys if leaked.
- **Data protection (Kenya DPA 2019 orientation):**
  - **consent** recorded at signup;
  - **export my data** at `/app/settings/privacy/export`: a ZIP containing JSON of every user-owned record, plus their uploaded CVs and generated kit files;
  - **delete my account:** confirmation, then a cascading delete of all user-owned records and files, removal of the inbound address, and an emailed confirmation;
  - **retention:** raw inbound email bodies after 30 days; LLMCall rows after 12 months; expired jobs older than 180 days with no user references are deleted;
  - **minimisation:** don't log prompt bodies in production.
  - The privacy policy must state that data is processed by the hosting provider (outside Kenya) and by the LLM provider. The owner is responsible for checking registration and cross-border requirements with the ODPC before public launch; include a TODO note in `docs/DEPLOYMENT.md`.
- **Audit:** log staff admin actions using Django's `LogEntry`. Log sensitive user actions (export, delete, Telegram link) with a lightweight `AuditEvent` model, or as structured log lines. Choose one and document it.

---

## 21. Coding conventions, logging and engineering practices

- Type hints everywhere, checked by **mypy** with django-stubs in CI (strict on `services.py`, `rules.py`, `pay.py`, `eligibility.py` and `validation.py`). Run `ruff` with a reasonable rule set (`E`, `F`, `I`, `B`, `UP`, `DJ`, `SIM`) and line length 100.
- **Services layer:**
  - business logic lives in `apps/<app>/services.py`, or in focused modules like `rules.py`, `pay.py`, `eligibility.py`, `validation.py`;
  - views and tasks contain no business logic;
  - services take explicit arguments (user, IDs or objects) and return domain results;
  - services raise domain exceptions (`QuotaExceeded`, `NotEligible`, `SourceError`, `LLMOutputError`) defined in `apps/<app>/exceptions.py`.
- **Transactions:** use `transaction.atomic()` for multi-object writes, and `transaction.on_commit()` before enqueueing Huey tasks that read the written data.
- **Settings:** access via `django.conf.settings`. Every new setting gets a default in `base.py` and an entry in `.env.example`.
- **Logging:** follow §21.1 exactly. No `print` outside management command output.
- **Templates:** `templates/base.html` and `templates/app_base.html`; HTMX partials prefixed with `_` (e.g. `_job_card.html`).
- **URLs:** namespaced per app.
- **Migrations:** one logical migration per phase where practical. Never edit an applied migration; add a new one.
- **Naming:** clear and boring.
- **Comments:** explain *why*, not *what*.
- **Docs:** keep `README.md` current with setup steps. Add a module docstring to every service module.

### 21.1 Logging and observability

**Goals:** when something goes wrong, the owner can answer "what happened, to which user/job/source, and why" from the logs plus the database event tables, without exposing personal data.

**Two layers of records:**
1. **Application logs** (stdout, short-lived): technical events for debugging. Defined below.
2. **Business event tables** (database, durable, queryable in the admin and staff pages): `SourceRun`, `LLMCall`, `Notification`, `ApplicationEvent`, `InboundEmail`, `AuditEvent`. These are the system of record for "did it happen?" and follow the retention rules in §20. Do not duplicate their full contents into logs; log a one-line summary with the record ID.

**Output and format:**
- Logs go to **stdout/stderr only** (twelve-factor). No log files written by the app.
- `LOG_FORMAT=json` (prod): one JSON object per line. `LOG_FORMAT=console` (dev): readable single-line text. `LOG_LEVEL` comes from env (default `INFO`).
- Implement in `apps/core/logging.py`:
  - a `JsonFormatter`;
  - a `ContextFilter` that injects context variables;
  - a `RedactionFilter`;
  - the `LOGGING` dict built in `settings/base.py`.
- **Standard fields on every line:**
  - `ts` (UTC ISO-8601 with milliseconds), `level`, `logger`, `event` (short snake_case event name, e.g. `source_run_finished`), `msg` (human text);
  - `env`, `release` (git SHA from `APP_RELEASE`), `service` (`web | worker | bot`);
  - `request_id` or `task_id`, `user_id` (numeric ID only), plus event-specific fields such as `source`, `job_id`, `kit_id`, `duration_ms`, `count`.
- Log with event names and extra fields, not string-formatted sentences:
  `logger.info("Source run finished", extra={"event": "source_run_finished", "source": s.name, "new_jobs": n, "duration_ms": ms})`.
  Provide a tiny helper `log_event(logger, level, event, msg, **fields)` to keep this consistent.

**Correlation:**
- `RequestIDMiddleware` (`apps/core/middleware.py`) reads `X-Request-ID` (set by Caddy in prod) or generates a UUID4. It stores the ID in a `contextvars.ContextVar`, binds `user_id` after authentication, and returns the ID in the response header.
- A `@logged_task` decorator wraps every Huey task. It:
  - binds `task_id`, the task name and the originating `request_id` (passed as an optional kwarg when the task was enqueued from a request);
  - logs `task_started` and `task_finished` with `duration_ms`, or `task_failed` with the exception;
  - reports exceptions to Sentry.
- The Telegram bot binds `chat` (an internal hash, not the chat ID) and `user_id` per update.

**Levels policy:**
| Level | Use for | Examples |
|---|---|---|
| DEBUG | Dev-only detail; off in prod | parsed field values, HTTP request URLs |
| INFO | Normal business events, one line each | source run finished, job ingested (count only), match scored, kit generated, notification sent, user signed up |
| WARNING | Recoverable problems | retry, source degraded, HTTP 429, LLM repair retry, quota exceeded, unparseable pay text, unrouted inbound email |
| ERROR | Failures needing attention (auto-sent to Sentry) | source failing, LLM output invalid after retry, kit generation failed, email send failed, unhandled exceptions |
| CRITICAL | System cannot function | database unreachable, missing required config at startup |

**What each area must log (minimum):**
| Area | Events |
|---|---|
| Sources | `source_run_started`, `source_run_finished` (ids_seen, new, updated, duration), `source_fetch_error` (status code, URL path only), `source_health_changed` |
| Ingestion | `jobs_ingested` (counts), `job_deduplicated` (job IDs), `job_expired_batch` (count) |
| LLM | `llm_call` (task, model, tokens, cost, latency, cache_hit, success) at INFO; `llm_repair_retry` WARNING; `quota_exceeded` WARNING. **Never the prompt or response bodies in prod** |
| Matching | `match_batch_finished` (job_id, users evaluated, passed, scored) |
| Kits | `kit_generation_started`, `kit_generation_finished` (kit_id, duration, validation issues count), `kit_validation_failed` |
| Notifications | `notification_sent` / `notification_failed` (kind, channel, notification_id), `digest_dispatch_finished` (users, sent) |
| Inbound email | `mailbox_poll_finished` (fetched, routed, ignored), `inbound_parse_failed` |
| Auth and accounts | `user_signed_up`, `login_failed` (IP + user_id if known, never the attempted password), `invite_used`, `data_exported`, `account_deleted` |
| Security | rate-limit hits, SSRF blocks, ownership-check 404s on per-user objects (WARNING, sampled to avoid noise) |

**Never log (enforced by `RedactionFilter` + code review):**
- passwords, password-reset or verification tokens, session IDs, API keys, OAuth secrets, IMAP passwords, Telegram bot token, `Authorization` and `Cookie` headers;
- **CV text, profile content, generated documents, cover letters, email bodies, pasted job text from users, LLM prompts and responses**;
- email addresses and phone numbers. Use `user_id`. If an email must be referenced (e.g. a waitlist entry), log the record ID.
- Telegram chat IDs. Log an internal hash instead.

The `RedactionFilter` masks values whose keys match `password|token|secret|api_key|authorization|cookie|session` (case-insensitive) in `extra` fields, and masks email-address patterns in messages, as a safety net. It does not replace care when writing log calls.

**Third-party loggers:**
- `httpx`, `httpcore`, `urllib3`, `PIL` and `weasyprint` set to WARNING.
- `django.request` at WARNING. `django.security` at WARNING.
- `django.db.backends` only at DEBUG in dev and only when explicitly enabled.
- **Access logs:** gunicorn access logging is off in prod; Caddy writes JSON access logs instead. Exclude `/healthz` from access logs.

**Errors (Sentry):**
- Initialise only when `SENTRY_DSN` is set, with:
  - `environment` (`SENTRY_ENVIRONMENT`), `release` (`APP_RELEASE`);
  - `send_default_pii=False`;
  - `traces_sample_rate` from env (default `0.0`, low if enabled);
  - a `before_send` hook that applies the same redaction rules.
- Tag events with `service` and `request_id` / `task_id`.
- Use the Django integration in web. Huey tasks report through the `@logged_task` decorator. The bot reports from its error handler.

**Retention and shipping:**
- **Production (Docker):** the `json-file` log driver with rotation, `max-size: 10m` and `max-file: 5` per service. That's about 50 MB per service, a few days of history.
- **Durable history** lives in the business event tables and Sentry.
- **Shipping logs** to a hosted service (Grafana Cloud or Better Stack free tiers) is in the backlog. The JSON format makes it a configuration change later.

**Health and metrics:**
- `/healthz` is shallow (process up + DB query), used by Caddy, Docker and UptimeRobot.
- `/healthz?deep=1` (staff or token only) also checks that the Huey DB is writable and the heartbeat is recent.
- The staff status page (§17.2) shows the operational numbers:
  - jobs ingested per source in the last 24h;
  - source health;
  - LLM spend today and this month;
  - notifications sent and failed;
  - kit failures;
  - queue backlog (pending Huey tasks).

**Tests:**
- the JSON formatter outputs valid JSON with the standard fields;
- the redaction filter masks secrets and emails;
- `request_id` propagates from middleware into logs and into tasks enqueued during the request;
- `@logged_task` logs start, finish and failure.

### 21.2 Engineering practices

**Version control:**
- **The owner handles all git operations.** Claude Code only edits files in the working tree and never stages, commits, branches, merges or pushes; the owner reviews `git diff`, then commits (on whatever branch they choose) and tags. `main` must always pass CI.
- **Conventional Commits** are the owner's convention when committing: `feat(sources): add jsonld fetcher`, `fix(kits): …`, `test(matching): …`, `docs: …`, `chore: …`.
- Protect `main` on GitHub (require CI to pass before merge) once the repo is on GitHub.
- At the end of each approved phase, the owner tags `v0.<N>.0`.

**Changelog and decisions:**
- **`CHANGELOG.md`** (Keep a Changelog format), updated every phase under `## [Unreleased]`, then moved under the version when tagged.
- **Architecture Decision Records** in `docs/adr/NNNN-title.md` (context, decision, consequences; one page each). Create these in Phase 0 from decisions already made:
  1. Django monolith + HTMX instead of SPA;
  2. Huey with SQLite storage instead of Celery + Redis;
  3. PostgreSQL in prod, SQLite in tests;
  4. shared job pool, per-user matches;
  5. forwarding addresses + one IMAP inbox instead of mailbox OAuth;
  6. Oracle Always Free ARM VM with Docker Compose;
  7. own thin LLM wrapper with an OpenAI-compatible provider + quotas;
  8. fabrication-checked CV generation (LLM selects, the database supplies facts).
  Add a new ADR whenever a phase makes a significant design choice or deviates from this spec.

**Quality gates (CI and the definition of done):**
- `ruff check`, `ruff format --check`, `mypy`, `makemigrations --check`, `pytest`, `pip-audit`, `gitleaks`.
- Coverage is reported; targets are in §19.
- **Dependabot** (or Renovate) is configured for Python dependencies, GitHub Actions and Docker base images, weekly, grouped. Updates arrive as PRs that must pass CI.

**Error handling:**
- Raise domain exceptions from services. Catch them **at boundaries** (views, tasks, bot handlers, management commands), where they become a user-facing message, a retry, or a logged failure.
- Never use a bare `except:`. Never silently swallow exceptions. If you catch to continue (e.g. one bad job in a batch), log a WARNING with context and continue.
- **Log an exception once**, where it's handled, with `exc_info`. Don't log and re-raise at every layer.
- User-facing errors are friendly and actionable. Technical details go to logs and Sentry, never to the page.

**Configuration:**
- All config comes from env (§22).
- `settings/prod.py` **fails fast at startup** with a clear message listing any missing required variables (`SECRET_KEY`, `DATABASE_URL`, `ALLOWED_HOSTS`, email settings, `LLM_*` when the provider isn't fake, and so on).
- Optional features (Telegram, inbound email, Sentry, Google sign-in) disable themselves cleanly when their variables are absent, logging one WARNING at startup.

**Database migrations:**
- Every migration is reversible where possible. Data migrations are separate from schema migrations.
- Destructive changes (dropping or renaming columns used in prod) follow two steps across phases: add the new field and backfill it, switch code over, then remove the old field later.
- Never edit an applied migration.

**Dependencies:**
- `uv.lock` is committed. Python, Postgres and Caddy versions are pinned in the Dockerfile and compose files.
- Add a new dependency only with a justification in the phase plan. Prefer well-maintained libraries.

**Code review:**
- Before posting a phase report, Claude Code runs a **self-review checklist**:
  - every per-user query is scoped;
  - nothing sensitive is logged;
  - new settings are in `.env.example`;
  - new tasks use `@logged_task` and are idempotent;
  - new external calls have timeouts and are mocked in tests;
  - user-facing strings are clear;
  - migrations are reversible;
  - the CHANGELOG and any ADRs are updated.
- The owner reviews the working-tree diff before committing.

---

## 22. Configuration and environment variables

`.env.example` must contain all of the following (with placeholders), grouped and commented:

```dotenv
# --- Django ---
DJANGO_SETTINGS_MODULE=jobpilot.settings.dev
SECRET_KEY=change-me
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://localhost:8000
SITE_URL=http://localhost:8000
SITE_NAME=JobPilot

# --- Database ---
DATABASE_URL=postgres://jobpilot:jobpilot@localhost:5432/jobpilot

# --- Storage / Huey ---
PRIVATE_MEDIA_ROOT=./var/private
HUEY_DB_PATH=./var/huey.db

# --- Email (outbound) ---
EMAIL_HOST=localhost
EMAIL_PORT=1025
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=False
DEFAULT_FROM_EMAIL=JobPilot <no-reply@example.com>

# --- Google sign-in ---
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# --- LLM ---
LLM_PROVIDER=fake                 # fake | openai_compat
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL_FAST=
LLM_MODEL_WRITER=
LLM_PRICES_JSON={}
LLM_LOG_BODIES=False
LLM_SYSTEM_DAILY_USD_CAP=1.00
QUOTA_DEFAULT_MONTHLY_SCORES=600
QUOTA_DEFAULT_MONTHLY_KITS=40
QUOTA_DEFAULT_MONTHLY_MANUAL=60
QUOTA_DEFAULT_MONTHLY_USD=3.00

# --- Telegram ---
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=

# --- Inbound email ---
INBOUND_EMAIL_DOMAIN=alerts.example.com
INBOUND_IMAP_HOST=imap.gmail.com
INBOUND_IMAP_USER=
INBOUND_IMAP_PASSWORD=

# --- Scraping ---
SCRAPER_USER_AGENT=Mozilla/5.0 (compatible; JobPilot/1.0; +https://example.com/bot)

# --- Logging ---
LOG_LEVEL=INFO
LOG_FORMAT=console                # console (dev) | json (prod)
APP_RELEASE=dev                   # set to git SHA in CI/CD builds

# --- Monitoring / backups (prod) ---
SENTRY_DSN=
SENTRY_ENVIRONMENT=development
SENTRY_TRACES_SAMPLE_RATE=0.0
HEALTHCHECKS_PING_URL=
BACKUP_RCLONE_REMOTE=
```

---

## 23. Implementation phases

Implement **one phase at a time** following `CLAUDE.md`. Each phase lists its goal, scope, acceptance criteria and required tests. The **first usable milestone for the owner** is the end of Phase 8 (jobs → scores → kits → Telegram alerts). Phases 11–12 must be complete before inviting friends.

---

### Phase 0 — Foundation and tooling
**Goal:** a clean, runnable Django project skeleton with all tooling in place.

**Scope:**
- uv project with `pyproject.toml`; ruff config; pre-commit config; pytest config.
- Django project `jobpilot` with split settings (`base`, `dev`, `test`, `prod`) using django-environ.
- `.env.example` (full list from §22), `.gitignore`, `README.md` with setup steps.
- **Custom User model** in `apps/accounts` (email as the unique login field) **before the first migration**.
- `apps/core`: `TimeStampedModel`, `UserOwnedModel` with the `for_user` manager, `/healthz` view (checks DB), sanitise helpers (stubs are fine).
- Huey configured (`huey.contrib.djhuey`, SqliteHuey), with one example periodic task (`core.heartbeat`, logging only).
- Tailwind standalone CLI setup script; `static/src/input.css`; base templates (`base.html`, `app_base.html`) with vendored HTMX and Alpine; a placeholder home page.
- `compose.yml` for dev: Postgres 16 + Mailpit (app services optional).
- `docker/Dockerfile`: multi-stage, Python 3.12 slim, WeasyPrint system libraries, builds Tailwind CSS, non-root user, works on **amd64 and arm64**. Not used for deployment yet, but it must build.
- GitHub Actions `ci.yml`: install with uv; ruff check and format check; mypy; `makemigrations --check`; pytest with coverage report; pip-audit; gitleaks.
- Test setup: `settings/test.py`, network-blocking fixture, `tests/factories.py` with a `UserFactory`.
- **Logging and observability foundation (§21.1):**
  - `apps/core/logging.py` (JSON and console formatters, context filter, redaction filter, `log_event` helper);
  - the `LOGGING` config driven by `LOG_LEVEL` and `LOG_FORMAT`;
  - `RequestIDMiddleware`;
  - the `@logged_task` decorator, used by the heartbeat task;
  - optional Sentry initialisation;
  - startup config validation in `settings/prod.py`.
- **Engineering practices (§21.2):**
  - mypy + django-stubs config;
  - pre-commit hooks (ruff, ruff-format, gitleaks, basic hygiene hooks);
  - `.github/dependabot.yml`;
  - `CHANGELOG.md`;
  - `docs/adr/` with ADRs 0001–0008 listed in §21.2;
  - work on branch `phase/0-foundation`.

**Acceptance criteria:**
- `docker compose up -d db mailpit`, `migrate` and `runserver` all work; `/healthz` returns 200 with `{"status":"ok"}`.
- With `LOG_FORMAT=json`, every request produces log lines containing `request_id`, and the `X-Request-ID` response header matches. Heartbeat task logs include `task_id` and `duration_ms`. A test proves the redaction filter masks a password, a token and an email address.
- `settings/prod.py` refuses to start with a missing `SECRET_KEY` and prints which variables are missing.
- All CI gates pass locally (ruff, mypy, migrations check, pytest, pip-audit, gitleaks).
- `run_huey` starts, and the heartbeat task logs every 10 minutes (verify with a shorter interval in dev, or a test).
- The custom user model is in use (`AUTH_USER_MODEL = "accounts.User"`).
- The Docker image builds locally (`docker build -f docker/Dockerfile .`).
- CI config is present. Tests pass: healthz test, `for_user` manager test, network-block test.

---

### Phase 1 — Accounts, invites, public pages
**Goal:** real authentication with invite-only signup, and a professional public face.

**Scope:**
- django-allauth: email login, mandatory verification, password reset, Google provider; styled templates.
- Models: `UserSettings`, `Invite`, `WaitlistEntry`, `ConsentRecord`, plus `UsageQuota` (model only; created here so signup can create it, with enforcement in Phase 2).
- Invite-only adapters (account + social), `/join/<code>/`, consent checkbox, the post-signup signal creating related objects. `InboundAddress` creation is deferred until its model exists (Phase 10), so leave a clear TODO.
- Admin: invites (bulk generate, copy link, email invite), waitlist approve action, user settings and quotas.
- Public pages: landing page with a waitlist form (rate-limited; Telegram notification to the owner deferred to Phase 8, so log for now), privacy policy, terms (with placeholders), custom 404 and 500 pages.
- App shell: authenticated layout with nav (mobile bottom nav + desktop sidebar), empty dashboard placeholder, settings → account page.
- Email templates (HTML + text) for verification, password reset and invites, branded.

**Acceptance criteria:**
- Signup without an invite is impossible: the signup URL and Google signup both refuse.
- With a valid invite, a user can sign up by email (verification email visible in Mailpit) or by Google. The invite becomes used, and `UserSettings` and `UsageQuota` exist.
- An invite locked to an email rejects other emails. Expired and used invites are rejected.
- Existing users can log in and log out, and reset their password.
- The waitlist form works and is rate-limited. Admin approve creates an invite and sends an email.
- Pages are responsive at 360px and desktop. 404 and 500 templates render.

**Tests:** invite validation cases; adapter logic; the signup signal creates related objects; waitlist rate limit; admin approve action; Google signup refused without invite (mock the social login flow at the adapter level).

---

### Phase 2 — LLM core layer and usage metering
**Goal:** a reliable, testable, cost-controlled LLM client used by every later phase.

**Scope:** everything in §11.1–§11.5:
- `LLMCall`, `LLMCache`; quota enforcement using `UsageQuota`;
- providers (`openai_compat`, `fake`), prompt loader with front-matter and versioning, structured output validation with a single repair retry, cost calculation from `LLM_PRICES_JSON`, system daily cap;
- untrusted-content wrapping helper (`wrap_untrusted(label, text, max_chars)`).
- **Settings → Usage** page (current month per task group vs quota, and USD spent).
- **Staff:** an LLM spend summary on the admin or the staff status page.
- A management command `llm_smoke` that runs one tiny structured call against the configured real provider (manual use only, never in tests).

**Acceptance criteria:**
- With `LLM_PROVIDER=fake`, structured calls return schema-valid objects, and an `LLMCall` row is logged with tokens and cost.
- Invalid JSON from the provider triggers exactly one repair retry, then `LLMOutputError`.
- Quota exceeded raises `QuotaExceeded` before any provider call. The system cap works.
- The cache returns stored results for the same key and prompt version without calling the provider.
- `llm_smoke` works against a real OpenAI-compatible endpoint when configured. The owner verifies this manually.

**Tests:** provider fake; validation and repair path (respx-mocked `openai_compat`); quota and cap logic; cost calculation; cache; prompt loader; untrusted wrapper truncation.

---

### Phase 3 — Profile, tracks and CV import
**Goal:** the user's master profile exists, is editable, and can be created from an uploaded CV.

**Scope:**
- Models from §5.3; `profile.version` bump on any change (signals or service-layer calls; prefer the service layer).
- Profile editor with HTMX inline CRUD and reordering; tracks CRUD with exactly one default; completeness meter.
- CV upload (validation per §20), text extraction, `parse_cv` LLM task (schema + prompt `parse_cv.v1`), review screen, apply with replace or merge.
- Onboarding wizard steps 1–4 (the later steps are added in Phases 8 and 10; the wizard must handle missing steps gracefully).
- Profile serialiser with stable IDs (`EXP-12`, `BUL-88`, …) in `profiles/serializers.py`, used later by scoring and kits. Also the compact profile summary builder (for scoring), cached per version.
- Extend the isolation tests.

**Acceptance criteria:**
- A user can upload a sample PDF CV and a DOCX CV, review the parsed data (from the Fake LLM fixture in dev), apply it, and see the profile populated.
- Scanned or empty PDFs give a friendly error.
- Every editor operation works on mobile width. Reorder persists.
- The serialiser output is stable and includes IDs. The summary builder is deterministic.
- The isolation suite covers all profile objects and CV imports.

**Tests:** text extraction from fixture PDF and DOCX files; parse → apply (replace and merge); version bumps; serialiser snapshot test; upload validation (wrong type, too large, magic bytes); isolation.

---

### Phase 4 — Source framework, ExpertWoka, ingestion
**Goal:** jobs flow automatically into the shared pool from ExpertWoka, with a framework ready for any future site.

**Scope:**
- Models: `Source`, `SourceRun`, `Job`, `JobReport`, `LLMCache` reuse.
- `RawJob`, `BaseSource`, registry with auto-import, generic fetchers: **`jsonld`** (full), **`json_api`** and **`rss`** (full, tested with synthetic fixtures), **`ats`** (Greenhouse and Lever adapters at minimum, endpoints verified), **`llm_extract`** (full, using Phase 2).
- **ExpertWoka source** configured per §6.5, with real saved fixtures (fetch 1 listing page and 3–5 detail pages **once**, manually, during development, and save them).
- Ingestion per §7: upsert, sanitise, pay parsing, eligibility normalisation, dedupe, scam heuristics, `extract_job` task (`JobExtraction` schema + prompt), `expire_stale` task.
- Scheduler tick, per-source locking, health tracking, staff notifications for failing sources (logged now, sent via Telegram after Phase 8).
- Admin for Source (run-now action, health display, recent runs inline) and Job (search, filters, raw JSON view).
- Management commands: `seed_sources`, `test_source <name> [--dry-run] [--limit N]`.
- Staff status page: sources with health, last run, new jobs in the last 24h; recent runs.
- `docs/SOURCES.md`.

**Acceptance criteria:**
- `test_source expertwoka --dry-run --limit 5` prints correctly parsed jobs from the live site: title, company, URL, locations, pay where present.
- With `run_huey` running, ExpertWoka is fetched on schedule. New jobs appear in the admin; re-runs don't create duplicates. Only new IDs trigger detail fetches (verify in the `SourceRun` counts).
- Pay parsing handles every example in §7.2. Eligibility returns correct results for Global, Kenya, a US-only listing, an EMEA listing and an unknown listing.
- Cross-source dedupe works (test with two synthetic sources).
- A fetcher pointed at a fixture with no jobs produces a degraded or failing health state, not a crash.
- ExpertWoka's terms were checked, and the result is noted in the phase report.

**Tests:** fixture-based ExpertWoka parser tests; generic fetcher tests; pay parser table tests; eligibility table tests; dedupe tests; `run_source` integration with respx; health transitions; expiry; scam heuristics.

---

### Phase 5 — Manual job input
**Goal:** users can add any job by URL or pasted text.

**Scope:** §8 in full: the URL path order (known source → JSON-LD → LLM extract), blocked domains list (LinkedIn etc.), SSRF protection, paste-text flow with a private job, rate limits, quota usage, the `/app/jobs/add/` page with two tabs. The created job goes to a simple detail page (the full detail page comes in Phase 6/7). Matching hooks: call the matching service for this user if it exists (Phase 6); otherwise leave a TODO with a no-op interface.

**Acceptance criteria:**
- An ExpertWoka URL uses the ExpertWoka fetcher.
- A page with JSON-LD is parsed without the LLM.
- An arbitrary page uses `extract_page`.
- Pasted text creates a private job visible only to its owner (isolation test).
- Private and localhost URLs are rejected. Oversized responses are rejected.
- A LinkedIn URL shows the "paste the description instead" message.

**Tests:** each path with respx; SSRF cases (including a DNS rebinding-style case where a hostname resolves to 127.0.0.1); private visibility isolation; rate limit.

---

### Phase 6 — Matching, scoring and the job feed
**Goal:** each user sees a ranked, relevant, eligible job feed.

**Scope:**
- `Match` model, rule filter (§10.2), LLM scoring (§10.3) with `MatchScore` + prompt, `match_new_job`, `rematch_user`, stale-score handling and a "Rescore" action.
- Dashboard feed (§10.5) with filters, HTMX pagination ("load more"), card actions (save, dismiss with reason, mark applied as a placeholder until Phase 9 creates an Application), and badges (eligibility, scam, expired, stale score, quota reached).
- Job detail page (description, strengths and gaps, rule reasons, source and alternate links, report listing).
- "Search now" (§10.6) with progress polling and a rate limit.
- Wire Phase 5 manual jobs into matching.

**Acceptance criteria:**
- With seeded jobs and a profile, the feed shows only rule-passed, eligible (or unclear) matches, sorted by score.
- Changing the track keywords and re-matching changes the feed. Stale scores are flagged.
- When the scoring quota is exhausted, matches show "Score limit reached" and no LLM call is made.
- Dismiss and save persist. "Search now" respects its rate limit and shows progress.
- The isolation suite covers matches and job detail access (including private jobs).

**Tests:** rule filter table tests (each condition); scoring service with the fake LLM; quota behaviour; feed view filters; search-now rate limit; isolation.

---

### Phase 7 — Application kit generation and documents
**Goal:** one click produces a tailored, honest, downloadable CV, cover letter, interview prep and screening answers.

**Scope:**
- Everything in §12: the `Kit` model, the generation pipeline with HTMX progress, the `CVData`, `InterviewItem` and screening schemas and prompts, style rules (`kits/style.py`), the **validator** (§12.5), PDF and DOCX rendering (classic template, bundled font), private file storage and ownership-checked downloads, the kit UI (preview, edit, re-render, regenerate with instruction, version dropdown, copy buttons), and auto-kit for high scores (hook ready for Phase 8 notifications).

**Acceptance criteria:**
- Generating a kit with the Fake LLM produces all four parts and four files (CV PDF and DOCX, cover letter PDF and DOCX). The PDF has selectable text and an ATS-friendly single column. File names follow the convention.
- Fabricated fixtures (an invented number, an unknown ID, a banned phrase, an em dash) are caught by the validator and dropped or repaired as specified.
- Company names, dates and titles in the documents come from the database, not the LLM.
- Editing a bullet and re-rendering updates the files without an LLM call.
- Regeneration creates version 2, and version 1 stays downloadable.
- Downloads by another user return 404.
- With a real provider configured, the owner generates one kit for a real job and reviews the tone. Report any prompt adjustments made.

**Tests:** validator unit tests (thorough); style checks; rendering smoke tests (PDF bytes start with `%PDF`; DOCX opens with python-docx and contains expected headings); pipeline with the fake LLM; quota; isolation for kits and files.

---

### Phase 8 — Notifications: Telegram and email
**Goal:** users get timely alerts on Telegram and/or by email, at the frequency they choose. **This completes the first usable milestone.**

**Scope:**
- Everything in §13:
  - models `TelegramLink`, `TelegramLinkToken`, `Notification`;
  - the bot (`run_bot` management command) with linking, commands, inline buttons with signed callback data, and kit document delivery;
  - email templates (digest, kit ready);
  - notification modes, quiet hours and the dispatcher;
  - instant sends after scoring; the auto-kit-then-notify flow (§10.4);
  - settings → notifications page;
  - onboarding step 5.
- Hook up the deferred items: staff Telegram notifications for failing sources and new waitlist entries.
- The Telegram bot is wrapped behind a small `TelegramGateway` interface, so tests use a fake.

**Acceptance criteria:**
- The owner can link their Telegram account from settings, receive an instant alert for a high-scoring match, tap "Kit" to receive the PDFs, tap "Applied ✓", and tap "Not interested". Each action is reflected in the dashboard.
- Daily digest mode sends exactly one digest at the chosen local time (Africa/Nairobi), and never duplicates on retries.
- Quiet hours defer instant alerts.
- `/pause` and `/resume`, `/status`, `/search` and `/digest` work. Unknown chats get the invite-only message. Callbacks from the wrong chat are rejected.
- Email digests render well in common clients (check in Mailpit).

**Tests:** due-time calculations across timezones and DST-free zones (time-machine); dispatcher idempotency via `dedupe_key`; callback signature and ownership checks; bot handlers with the fake gateway; quiet hours.

---

### Phase 9 — Application tracker
**Goal:** users track every application, and outcome data is captured cleanly.

**Scope:** everything in §14: `Application`, `ApplicationEvent`, `Interview`, the `change_stage` service, kanban with SortableJS (and a dropdown fallback on mobile), application detail and timeline, manual applications, follow-up reminders (§13.5), the stats page. Wire up "Mark applied" from the feed, the job detail page and Telegram.

**Acceptance criteria:**
- Applying from any entry point (feed, detail, Telegram) creates exactly one Application linked to the latest kit.
- Drag and drop changes the stage and records an event. The mobile dropdown does the same.
- Follow-up reminders are sent once and rescheduled. The ghosted suggestion appears after 21 days.
- Stats compute correctly on seeded data.
- Isolation covers applications, events and interviews.

**Tests:** `change_stage` events; uniqueness; reminders with time-machine; stats calculations; views; isolation.

---

### Phase 10 — Email job-alert ingestion
**Goal:** users forward LinkedIn and other job-alert emails, and those jobs flow into their feed.

**Scope:** everything in §9:
- `InboundAddress` (create one for existing users with a data migration, and on signup);
- `InboundEmail`, `ForwardingConfirmation`;
- a `MailboxClient` interface with an IMAP implementation (imap-tools) and a fake;
- the `poll_mailbox` task;
- token routing, Gmail confirmation detection with a dashboard banner and a Telegram notice;
- the parser registry (`LinkedInAlertParser`, `GenericAlertParser`);
- labels or folders (processed, unrouted);
- the settings → job alerts page with the address, copy button, Gmail and Outlook instructions, and recent emails;
- onboarding step 6.
- Write `docs/EMAIL_SETUP.md` explaining the owner's one-time setup: Cloudflare Email Routing catch-all → dedicated Gmail, IMAP app password, labels.

**Acceptance criteria:**
- Feeding fixture `.eml` files through the fake mailbox:
  - a LinkedIn alert creates the right number of jobs with titles, companies and LinkedIn URLs, and no LinkedIn page is fetched;
  - the receiving user gets matches.
- A Gmail confirmation email produces a visible confirmation banner with a working link for the right user only.
- An unrouted email is ignored and moved.
- Duplicate `Message-ID`s are processed once.
- With real credentials, the owner forwards a real alert and sees the jobs appear (manual check).

**Tests:** parser fixture tests; routing by headers; confirmation extraction; idempotency; isolation for inbound emails and confirmations.

---

### Phase 11 — Privacy, security hardening, polish
**Goal:** safe to invite friends.

**Scope:**
- Data export ZIP; account deletion flow (cascade + files + confirmation email); the retention cleanup task (§20).
- Production security settings; CSP (Alpine CSP build if needed); all rate limits from §20 verified; upload hardening review.
- Audit events for export, delete and Telegram link.
- Review every view for ownership checks; complete the isolation suite.
- Update the privacy policy content to match the actual implementation.
- UI polish pass: empty states, loading states, error messages, mobile layout review of every page, accessibility review (labels, contrast, focus).
- **Logging audit:**
  - grep every log call added so far and confirm none logs PII or secrets (§21.1 "Never log");
  - confirm every Huey task uses `@logged_task`;
  - confirm every area emits the minimum events in the §21.1 table;
  - add any that are missing, plus a test that runs a full flow (ingest → score → kit → notify) with log capture and asserts no email addresses or CV text appear in the logs.
- Performance: add indexes for feed queries; `select_related` / `prefetch_related` in the feed and tracker. Check query counts with django-debug-toolbar and assert them in tests for the feed (e.g. under 15 queries per page).

**Acceptance criteria:**
- Export contains all of the user's data and files and nothing from other users.
- Deletion removes everything belonging to the user, verified by tests counting rows and files.
- `manage.py check --deploy` with prod settings has no warnings except documented, accepted ones.
- Retention removes old email bodies and keeps metadata.
- The isolation suite covers every per-user URL. A test enumerates URL patterns and fails if a new `app/` URL isn't covered by an isolation test or explicitly exempted.

**Tests:** export contents; deletion completeness; retention with time-machine; URL-coverage meta-test; query-count tests.

---

### Phase 12 — Production deployment
**Goal:** the app runs on the Oracle Cloud Always Free ARM VM with HTTPS, CI/CD, backups and monitoring.

**Scope:**
- `compose.prod.yml`: caddy, web (gunicorn, 2–3 workers), worker (`run_huey`), bot (`run_bot`), db (Postgres 16, data volume). Shared volumes for the Huey database and private media. Healthchecks on each service. Restart policies. Memory limits that suit the VM.
- **Log settings in prod:**
  - `LOG_FORMAT=json` and a per-service `SERVICE_NAME` (`web`, `worker`, `bot`);
  - the `json-file` log driver with `max-size: 10m`, `max-file: 5` on every service;
  - `APP_RELEASE` set to the git SHA at image build time (build arg → env) and used by logs and Sentry.
- `docker/Caddyfile`: domain from env, automatic HTTPS, reverse proxy to web, security headers, gzip and zstd. It also generates `X-Request-ID` (Caddy's `{http.request.uuid}`) when the request lacks one, forwards it to Django, writes JSON access logs, and skips logging for `/healthz`.
- `docker/entrypoint.sh`: wait for the database, migrate (web only), collectstatic.
- `.github/workflows/deploy.yml`:
  1. on push to `main` after CI passes, build a **linux/arm64** image (plus amd64) with buildx;
  2. push it to GHCR;
  3. SSH to the VM (secrets: host, user, key);
  4. `docker compose pull && docker compose up -d`;
  5. run a post-deploy health check of `/healthz`.
- **Backups:** the `core.backup_db` task (or a cron script) runs `pg_dump` (compressed) plus a private media archive, uploads to an rclone remote (Cloudflare R2 or Oracle Object Storage), and keeps 14 daily and 8 weekly copies. `scripts/restore.sh`, with documented restore steps. Do one test restore on a local machine.
- **Monitoring:**
  - Sentry (web, worker, bot) when the DSN is set;
  - `core.heartbeat` pings `HEALTHCHECKS_PING_URL`;
  - separate ping URLs for backups and source runs are optional;
  - document UptimeRobot setup for `/healthz`.
- `docs/DEPLOYMENT.md`, a complete runbook:
  1. Oracle account and region notes (ARM capacity, idle-reclaim risk, current free allocation);
  2. creating the VM (Ubuntu 24.04 aarch64);
  3. firewall and security lists (ports 80 and 443 only, SSH restricted);
  4. installing Docker;
  5. domain DNS on Cloudflare (A record; proxy settings compatible with Caddy's certificate issuance);
  6. email DNS: SPF, DKIM and DMARC for the outbound provider, and the Cloudflare Email Routing catch-all for inbound alerts;
  7. Google OAuth client setup (authorised redirect URIs; consent screen with privacy and terms URLs; testing vs production publishing);
  8. Telegram bot creation via BotFather;
  9. the first deploy;
  10. creating the owner's superuser and first invites;
  11. rotating secrets;
  12. restoring from backup;
  13. the ODPC/data-protection TODO before any public launch.
- **Fallback documentation:** running on a 1 GB VM (GCP e2-micro), with reduced gunicorn workers, swap, and SQLite as an optional database.

**Acceptance criteria:**
- `docker compose -f compose.prod.yml up` works locally with prod-like settings: HTTPS via Caddy's internal CA locally, or HTTP for the local test.
- The CI build produces arm64 images.
- Backup and restore round trip verified locally.
- The owner can follow `docs/DEPLOYMENT.md` start to finish. Ask the owner to perform the real deployment steps and report back; assist with any issues.
- After deployment: HTTPS works, sign-in with Google works on the real domain, ExpertWoka fetching runs on schedule, Telegram alerts arrive, and the heartbeat is visible in Healthchecks.

---

## 24. DevOps and deployment (reference)

- **Target host:** Oracle Cloud Always Free, Ampere A1 (ARM64), Ubuntu 24.04. The free ARM allocation was reduced in mid-2026 (from 4 OCPU / 24 GB to 2 OCPU / 12 GB). That is still far more than needed; the target footprint is under 2 GB of RAM. Risks: ARM capacity shortages when creating instances, no SLA, possible reclaiming of idle instances, terms may change. **Off-server backups make these risks acceptable.**
- **Fallback host:** GCP e2-micro free tier (1 GB RAM): fewer workers, swap enabled, optionally SQLite.
- **Avoid free PaaS tiers that sleep.** They break scheduled fetching and the Telegram bot.
- **Costs:** a domain (about USD 10–15/year) and LLM usage (bounded by quotas). Everything else uses free tiers:
  - Cloudflare DNS, Email Routing and R2;
  - a transactional email provider's free tier;
  - Sentry, Healthchecks.io and UptimeRobot free tiers;
  - GitHub Actions and GHCR.
- **CI:** every push runs lint, migrations check and tests. Deploys only from `main`.
- **Zero-downtime deploys** aren't required. A few seconds of restart is acceptable.

---

## 25. Backlog (do NOT implement until the owner schedules it)

- More sources: micro1 direct (JSON endpoint from DevTools), Turing, TELUS Digital careers, Remotive and other remote boards, Kenyan boards (BrighterMonday, Fuzu, MyJobMag) via email parsers, ATS boards of chosen companies.
- WhatsApp notifications (Meta Cloud API; templates; paid per conversation).
- Insights and recommendations from anonymised outcomes (opt-in, minimum group size of 10 before showing any stat).
- Additional CV templates; a cover letter tone selector.
- OCR for scanned CVs and job-advert images.
- A browser extension ("Save this job").
- PWA with push notifications; a Flutter mobile app (would need a JSON API built on the existing services layer, e.g. Django Ninja).
- Gmail/Outlook OAuth mailbox reading (requires restricted-scope verification).
- Billing and plans via M-Pesa (Daraja API).
- MFA (allauth.mfa).
- PostgreSQL row-level security as defence in depth.
- Currency conversion for pay comparisons.
- A mock interview practice mode.
- Learning from dismiss reasons and outcomes to tune scoring automatically.
- Log shipping to a hosted log service (Grafana Cloud / Better Stack free tier) with alerts on ERROR rates.
