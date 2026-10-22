# 9. Invite-only signup: consent on the signup form, shared rate-limit store

- Status: Accepted
- Date: 2026-10-22

## Context

Signup must be impossible without an invite, for email and Google alike, and must record consent
to the terms and privacy policy (spec §15.2, §20). Rate limits (§20) must hold across gunicorn
workers and behind Caddy.

## Decision

- Both allauth adapters return `is_open_for_signup=True` only when the session holds a valid
  invite code (set by `/join/<code>/`). Existing users are never affected, because allauth asks
  only when a *new* account would be created.
- Consent is a required checkbox on the signup form (`ACCOUNT_SIGNUP_FORM_CLASS`). For Google we set
  `SOCIALACCOUNT_AUTO_SIGNUP = False`, so a new Google user lands on the same form and cannot
  skip the checkbox. One extra click is the price for server-enforced consent.
- `Invite` email locks are enforced in `clean_email` (email form) and `is_open_for_signup`
  (Google). The invite is claimed with a conditional `UPDATE ... WHERE used_at IS NULL` so two
  racing signups cannot both use it.
- `SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True` (beyond the spec's list): Google
  verifies emails, and the spec says existing users can log in with Google if the email matches.
- Rate limits use django-ratelimit. In production the counters live in a `DatabaseCache`
  (`createcachetable` runs in the entrypoint) because a per-process cache would multiply every limit
  by the worker count. The client IP is the last `X-Forwarded-For` entry (the one Caddy appended).
- `UsageQuota` lives in `apps.llm` (spec §5.11) and is created at signup; enforcement is Phase 2.
- allauth's pages are styled by overriding its `allauth/layouts/*` and `allauth/elements/*`
  templates, so every allauth page, including future ones, inherits the app look.

## Consequences

Consent cannot be bypassed on either path. A database cache adds a small write per limited request,
which is negligible at this scale. If the project later moves to Redis, only `CACHES` changes.
