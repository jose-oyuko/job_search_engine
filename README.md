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
