# 14. Matching: rules first, lazy scoring, derived verdicts, debounced re-matching

- Status: Accepted
- Date: 2026-10-23

## Context

Phase 6 turns the shared job pool into a ranked, per-user feed (spec §10). LLM scoring costs money
and users have a monthly allowance, so most of the design is about when *not* to call the model.

## Decision

- **Deterministic rules decide who sees what; the LLM only ranks.** `apps/matching/rules.py` is pure
  (no database, no model) and records a plain-words reason for every decision. Unknown facts never
  exclude a job: they pass with a note. A `Match` row exists only for jobs that passed (spec §10.1),
  except jobs a user added themselves, which are always kept and flagged "outside your target".
- **Users with no job target get no matches** (the dashboard asks them to create one). A track with
  no keywords can still match through skills, as the spec's second relevance clause says.
- **Pay is compared only in the same currency** and a comparable period (same period, or month and
  year converted exactly); anything else passes with the reason "pay not comparable".
- **Scoring is queued per match** (`score_match_task`), after commit, and never in the request. It is
  skipped without calling the provider when the allowance is used up, which the UI shows as "Score
  limit reached" (derived from the quota, not stored). The input is hashed into the cache key, so an
  identical profile, track and job costs nothing the second time, and keys include the user.
- **The verdict is derived from the score** (80/60/40), not trusted from the model, so the number and
  the label can never disagree. Scores are clamped to 0-100; strengths, gaps and the summary are
  trimmed to the documented lengths.
- **Stale scores are computed, not stored:** `scored_profile_version != profile.version`. Re-matching
  after a profile or track change re-runs the rules only (cheap). Stale and unscored matches are
  refreshed lazily when viewed (once per 10 minutes at most), by "Rescore", or by "Search now".
- **Re-matching is debounced.** Every profile version bump schedules `rematch_user_task` 30 seconds
  later; further edits in that window find it pending (`cache.add`) and share the run. A new user's
  first re-match also scores up to 10 matches so the first dashboard is not empty.
- **New jobs:** `extract_job_task` now enqueues `match_new_job_task` whether or not extraction worked.
  Duplicates, private and expired jobs are never matched this way.
- **"Search now":** a 10-minute cooldown stored in `UserSettings.last_manual_search_at` (so it
  survives cache clears), sources not run in the last 5 minutes are queued, and the user's recent
  stale and unscored matches are rescored within the allowance. Progress is derived from `SourceRun`
  rows and new matches since the start time, with the start state kept in the cache.
- **After-score hooks** (`apps/matching/hooks.py`) are no-ops until Phases 7 (auto kit) and 8
  (instant notification).
- **`pages:dashboard` stays the URL name** for `/app/`; it now renders the feed.

## Consequences

The money-spending step is isolated and easy to reason about. Scoring quality depends on the prompt and
the profile summary, which is why both are versioned. The rules can be wrong in ways the user cannot
see unless they open "Why this job was shown to you", which lists them.
