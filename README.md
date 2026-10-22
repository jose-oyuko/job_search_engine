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
