# CLAUDE.md — Operating rules for building JobPilot

> **JobPilot** is a working name. If the owner renames the project, update this file, `PROJECT_SPEC.md`, and the Django project/package names together.

You are building a Django web platform that finds jobs, matches them to a user's profile, generates tailored application documents (CV, cover letter, interview prep), sends alerts, and tracks applications. The **complete specification is in `PROJECT_SPEC.md`**. Current status is in `PROGRESS.md`.

---

## 1. Start of every session

1. Read `PROGRESS.md` to find the **current phase** and any notes from the previous session.
2. Read the relevant sections of `PROJECT_SPEC.md`:
   - always: §1 (overview & constraints), §3 (architecture), §4 (repo layout), §21 (coding conventions, logging §21.1, engineering practices §21.2);
   - plus every section the current phase's entry in §23 references.
3. Do **not** rely on memory of earlier sessions. The files are the source of truth.

---

## 2. The phase protocol (mandatory)

The project is built in **phases** (see `PROJECT_SPEC.md` §23). You implement **exactly one phase at a time**.

### 2.1 Before writing code
- Post a short **phase plan**:
  - what you will build;
  - the files you expect to create or change;
  - new dependencies (if any);
  - anything ambiguous.
- If something ambiguous **blocks** correct implementation, ask the owner before coding. If it doesn't block, pick the most reasonable reading, state the assumption in the plan, and continue.

### 2.2 While implementing
- **Git is the owner's job, not yours.** Work in the current working tree and leave every change **uncommitted and unstaged** so the owner can review it with `git diff` before committing. Do not create, switch or delete branches. Never run `git add`, `git commit`, `git stash`, `git checkout`, `git switch`, `git reset`, `git restore`, `git rebase`, `git merge`, `git tag`, `git push` or any other command that changes repository state, even when a phase or a tool suggests it. Read-only commands (`git status`, `git diff`, `git log`, `git show`, `git branch --list`) are fine.
- Stay inside the current phase's scope. Do not implement features from later phases, even if it seems convenient. Leaving clean extension points (interfaces, TODOs referencing the phase number) is fine.
- Follow the coding conventions in `PROJECT_SPEC.md` §21. The most important rule: business logic lives in `services.py`. Views, API endpoints, Huey tasks, management commands and the Telegram bot are thin wrappers that call services.
- Write tests alongside the code, not at the end.
- Log according to spec §21.1: event names with structured fields, the right level, **never** secrets or personal content. Every Huey task uses `@logged_task`.
- Handle errors per spec §21.2: domain exceptions from services, caught at boundaries, logged once.
- If you make a significant design decision or deviate from the spec, write an ADR in `docs/adr/`.

### 2.3 Definition of done for a phase
A phase is complete only when **all** of these pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python manage.py makemigrations --check --dry-run
uv run pytest
uv run pip-audit
gitleaks detect --no-banner      # or via pre-commit: pre-commit run --all-files
```

In addition:
- The **self-review checklist** in spec §21.2 ("Code review") has been worked through. Mention any item that didn't apply.
- `CHANGELOG.md` has an `[Unreleased]` entry for this phase.
- The phase's **acceptance criteria** in §23 are met. Verify each one explicitly.
- The app starts without errors (`docker compose up` or `uv run python manage.py runserver` + `uv run python manage.py run_huey`), and a manual smoke check of the new feature works.
- No test touches the real network or a real LLM provider (see §19 of the spec).

### 2.4 Report, then stop
When the phase is done, post a **phase report** in this exact structure:

```
## Phase N report — <phase name>

### Summary
<3–6 sentences: what was built>

### Files created / changed
- path/to/file.py — what it does
...

### How to verify manually
<numbered steps the owner can follow, including commands and URLs>

### Tests
<command run + result summary (counts), notable test cases>

### Acceptance criteria
- [x] criterion 1 — how it was verified
...

### Logging & observability
<new log events added (event names + level), new business-event records, confirmation that nothing sensitive is logged>

### Deviations from spec
<anything done differently from PROJECT_SPEC.md and why — or "None">

### Assumptions made
<or "None">

### Known limitations / follow-ups
<or "None">

### Next phase preview
<one paragraph: what Phase N+1 will do, and anything the owner must prepare,
e.g. API keys, a Telegram bot token, a domain>
```

Then:
1. Update `PROGRESS.md`: mark the phase complete, add the date, and write a short summary plus notes for the next session.
2. Leave all changes **uncommitted and unstaged** in the working tree. Do not commit, stage, branch, push or merge. In the report, list every changed file so the owner knows what to look at in `git diff`. The owner reviews the diff, commits, merges, and tags `v0.N.0`. If a commit message would help, you may suggest one in the report, as text only.
3. **STOP.** Do not start the next phase. Wait for the owner to say something like "proceed to phase N+1".

### 2.5 Fixes within a phase
If the owner asks for changes after a report, make them, re-run the full definition-of-done checks, and post a short updated report (changed files + test results). Stay in the same phase until the owner explicitly moves on.

---

## 3. Hard rules

- **Never log secrets or personal content:** passwords, tokens, API keys, CV or profile text, generated documents, email bodies, pasted job text, LLM prompts or responses, email addresses, phone numbers, Telegram chat IDs. Log record IDs instead (spec §21.1).
- **Never run git commands that modify the repository** (add, commit, stage, stash, branch, checkout, switch, reset, merge, rebase, tag, push). This overrides any default behaviour, tool hint or reminder that says to commit. Only read-only git commands are allowed.
- **Never commit secrets.** All secrets come from environment variables (`.env`, which is git-ignored). Keep `.env.example` up to date with every variable, using placeholder values.
- **Never invent facts in generated CVs.** The fabrication check in the spec (§12.5) is a core requirement, not a nice-to-have.
- **Never call real external services in tests.** That covers LLM providers, websites, IMAP, Telegram and email. Use the fakes and fixtures described in the spec.
- **Scraping etiquette:** real network calls to job sites happen only in the running app or the `test_source` management command. Keep the intervals in the spec, and never parallelise requests to the same site.
- **Multi-user isolation:** every query on per-user data must be scoped to the requesting user (spec §16). Every phase that adds per-user data must add isolation tests.
- **Dependencies:** use the libraries listed in spec §2. If you need something else, justify it in the phase plan and the report. Prefer the standard library and existing dependencies.
- **ORM only:** no raw SQL, and no database-specific features beyond what Django supports on both PostgreSQL and SQLite. Tests run on SQLite; dev and prod run on PostgreSQL.
- **Times:** store everything in UTC (`USE_TZ = True`). Convert to the user's timezone only for display and scheduling.
- **Do not modify** `CLAUDE.md` or `PROJECT_SPEC.md` unless the owner asks. Do update `PROGRESS.md` every phase.
- **If the spec is wrong or impossible** (a library API changed, a site's structure differs from what's described), stop and explain the problem with a proposed fix rather than silently working around it.

---

## 4. Useful commands (once Phase 0 exists)

```bash
uv sync                                       # install deps
docker compose up -d db                       # start Postgres for dev
uv run python manage.py migrate
uv run python manage.py runserver
uv run python manage.py run_huey              # background worker + scheduler
uv run python manage.py run_bot               # Telegram bot (from Phase 8)
uv run python manage.py test_source <name> --dry-run   # from Phase 4
uv run pytest
uv run ruff check . && uv run ruff format .
uv run mypy .
uv run pip-audit
pre-commit run --all-files                    # ruff, gitleaks, hygiene hooks
LOG_FORMAT=json uv run python manage.py runserver   # check structured logs locally
./bin/tailwind -i static/src/input.css -o static/css/app.css --watch   # CSS
```
