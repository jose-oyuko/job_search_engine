# 4. Shared job pool, per-user matches

- Status: Accepted
- Date: 2026-10-21

## Context

Several users follow the same sources. Fetching and extracting each job per user would multiply scraping load and LLM cost.

## Decision

Store jobs once in a shared pool (fetched on each source's interval). Matching, scoring, kits, notifications and applications are per user and inherit `UserOwnedModel`. A job pasted by a user is private to them (`visibility = private`).

## Consequences

Cheaper and politer to sources. Tenancy is enforced in code (`for_user`, `visible_to`) and verified by an isolation test suite rather than by database row-level security, which stays in the backlog.
