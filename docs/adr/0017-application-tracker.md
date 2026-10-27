# 17. Application tracker: one application per job, events as history, reminders via notifications

- Status: Accepted
- Date: 2026-10-26

## Context

Phase 9 lets users track applications from "applied" to an outcome and captures clean outcome data
(spec §14). "Mark applied" existed in three places (feed, job page, Telegram) and only set the match
status, so the tracker had to become the single place that creates an application.

## Decision

- **`tracker.services.apply_to_job` is the only way an application is created.** The feed, the job
  page, the Telegram button and the manual form all call it. A unique constraint on (user, job) plus
  a get-or-create makes a double tap, two entry points, or two browser tabs produce exactly one
  application and one first event. Applying again to a `saved` application moves it to `applied`;
  anything further along is left alone.
- **Linking is automatic and editable.** The latest ready kit for the job is linked when the
  application is created, the match (if any) becomes `applied`, the match's track is copied, and the
  first follow-up is set 7 days out. The user can pick another ready version, or none, on the detail
  page; only a ready kit of theirs for the same job is accepted.
- **Stage history is append-only events.** `change_stage` writes an `ApplicationEvent` (from, to,
  time, note) and updates `last_stage_change_at`. Moving out of `applied` clears the follow-up date;
  moving into it sets one. Events are never edited, so response rates and "days to first response"
  can be computed from them later without trusting the current stage alone.
- **Response means the employer answered:** the application ever reached screening, interview,
  offer, hired or rejected. Ghosted and withdrawn are not responses. Saved applications are not
  counted as sent. Stats are plain Python over the user's own rows.
- **Manual applications create a private `Job`** (owner = the user), so every application has a job
  and the unique constraint, kit links and stats work the same way. No match is created for it.
- **Restoring a dismissed or applied match** from the feed does not delete its application: the
  history stays, and the board is where an application is moved or ended.
- **Reminders reuse the notification path.** `tracker.followup_reminders` (daily, 07:00 UTC) queues a
  `followup_reminder` notification per channel for each applied application whose follow-up date has
  passed, then moves the date 7 days on. After 21 days without a change it queues one "consider
  marking it ghosted" suggestion. Payloads hold the application ID only; the text is built at send
  time, and a reminder is skipped if the application has moved on since. Dedupe keys include the
  follow-up date, so a second run the same day adds nothing. Users with alerts off get no reminders
  (their dates still advance); quiet hours defer them.
- **Board:** SortableJS (vendored, MIT) posts the new stage with HTMX and the server returns the
  whole board, so column counts stay right; each card also has a stage dropdown shown on phones, and
  a plain POST fallback redirects only to the board or the application (never to a caller-supplied
  address).
- **Interview times** are typed in the user's own time zone and stored in UTC.
- **URLs** are under `/app/tracker/` (spec §17.2), not the shorter `/tracker/` in §14.

## Consequences

The tracker is the source of truth for "applied"; the match status mirrors it. A reminder is at most
a day late (the task is daily). Drag and drop needs JavaScript; without it the dropdown and the
detail page still work.
