# JobPilot

Finds jobs, matches them to your profile, generates tailored application kits (CV, cover letter,
interview prep), sends alerts, and tracks applications. "JobPilot" is a working name.

- `PROJECT_SPEC.md` — what to build · `CLAUDE.md` — how we work · `PROGRESS.md` — current status
- Architecture decisions: `docs/adr/`

## Requirements

- [uv](https://docs.astral.sh/uv/) (it provisions Python 3.12)
- Docker (for Postgres and Mailpit in development)
- [gitleaks](https://github.com/gitleaks/gitleaks#installing) for the secret-scan hook

## Setup

```bash
uv sync                                  # install dependencies
cp .env.example .env                     # then edit; set a real SECRET_KEY
docker compose up -d db mailpit          # Postgres on :5432, Mailpit UI on http://localhost:8025
uv run python manage.py migrate
uv run python manage.py createsuperuser
bash scripts/setup_tailwind.sh           # downloads the Tailwind CLI into bin/
./bin/tailwind -i static/src/input.css -o static/css/app.css   # add --watch while styling
uv run python manage.py runserver        # http://localhost:8000, health: /healthz
uv run python manage.py run_huey         # background worker + scheduler (second terminal)
pre-commit install                       # ruff, formatting, gitleaks and hygiene hooks
```

On Windows the CLI is saved as `bin/tailwind.exe`.

If port 5432 is already taken by a local Postgres, set `DB_PORT=5544` in `.env` and use the same
port in `DATABASE_URL`.

Set `HEARTBEAT_INTERVAL_MINUTES=1` in `.env` to watch the worker heartbeat in the logs quickly.
Use `LOG_FORMAT=json` to see the structured production log format locally.

## Accounts and invites

Signup is invite-only. Create invites in the admin (`/admin/` → Invites → "Generate invites", or
approve a waitlist entry, which emails an email-locked invite). Verification and invite emails show
up in Mailpit at http://localhost:8025.

Google sign-in: create an OAuth client at https://console.cloud.google.com/apis/credentials, add
`http://localhost:8000/accounts/google/login/callback/` as an authorised redirect URI, and put the
client ID and secret in `.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`). Without them the
button is hidden.

## LLM layer

`LLM_PROVIDER=fake` (the default) runs the whole app without an API key. To use a real
OpenAI-compatible provider set `LLM_PROVIDER=openai_compat`, `LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL_FAST`, `LLM_MODEL_WRITER` and `LLM_PRICES_JSON` in `.env`, then check it:

```bash
uv run python manage.py llm_smoke
```

Per-user usage is at `/app/settings/usage/`; staff see spend under Admin → LLM calls.

## Profile and CV import

Your master profile lives at `/app/profile/`. Importing a CV (PDF or DOCX) needs the background
worker: run `uv run python manage.py run_huey` in a second terminal, otherwise the upload stays on
"Reading your CV". With `LLM_PROVIDER=fake` the parsed result comes from
`tests/fixtures/llm/parse_cv.json`, so any readable CV shows the same sample data in development.

## Job sources

```bash
uv run python manage.py seed_sources                         # creates the ExpertWoka source
uv run python manage.py test_source expertwoka --dry-run --limit 5   # live check, saves nothing
uv run python manage.py run_huey                             # fetches on schedule (every 20 min)
```

Sources, runs and jobs are in the admin; source health and recent runs are at `/app/staff/status/`
(staff only). See `docs/SOURCES.md` to add a site. Set `SCRAPER_USER_AGENT` to include a real contact
URL before running against live sites.

## The job feed

`/app/` ranks the jobs that fit your job targets (tracks), best first. New jobs are matched by the
worker (`run_huey`); each match is scored by the LLM within your monthly allowance (Settings → Usage).
"Search now" refreshes sources and rescoring (once every 10 minutes). Changing your profile or a
track re-runs the rules after about 30 seconds and marks older scores as out of date.

## Application kits

On a job page, "Generate kit" writes a CV and cover letter for that job from your profile, plus
interview questions and screening answers. Every CV claim cites a profile item and is checked: an
invented number, a skill you do not have, or a banned phrase is removed, never published. Download
PDF or Word files, edit the text and rebuild without using any AI allowance, or regenerate with an
instruction (a new version; old ones stay). Jobs scoring at or above your auto-kit threshold get a
kit automatically. The worker (`run_huey`) does the writing.

PDFs need WeasyPrint's Pango libraries. The Docker image has them. On Windows install the GTK
runtime (`winget install tschoonj.GTKForWindows`), then open a new terminal. Without it, kits still
generate and the Word files work; use "Re-render documents" once Pango is installed. To regenerate
the Word templates after editing `apps/kits/docx_templates.py`, run
`uv run python manage.py build_doc_templates`.

## Adding a job by hand

`/app/jobs/add/` takes a link or a pasted description. Links are fetched through an SSRF-safe reader
(public addresses only); LinkedIn, Indeed and similar sites cannot be read, so paste the description
for those. Each user can add 20 jobs a day.

## Checks (all must pass before a phase is done)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python manage.py makemigrations --check --dry-run
uv run pytest
uv run pip-audit
gitleaks detect --no-banner
```

Tests use SQLite in memory, a fake LLM, eager Huey and a guard that blocks non-local network
access.

## Docker image

```bash
docker build -f docker/Dockerfile -t jobpilot .
```
