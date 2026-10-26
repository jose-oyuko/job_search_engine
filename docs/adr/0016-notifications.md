# 16. Notifications: one idempotent delivery path, signed buttons, a gateway for Telegram

- Status: Accepted
- Date: 2026-10-26

## Context

Phase 8 sends job alerts by Telegram and email (spec §13). Alerts must never be sent twice, must
respect quiet hours and digest times in the user's own time zone, and the bot must not let one user
act on another's jobs. Telegram cannot be reached from tests.

## Decision

- **Everything goes through `Notification` rows.** `create_notification` inserts one row per user,
  channel and `dedupe_key` (a unique constraint), and `deliver` claims the row with a conditional
  update, sends, and records the outcome. A retry, a second worker, the dispatcher running twice or a
  repeated button press cannot send the same alert twice. A claim older than ten minutes is treated
  as a dead worker's and can be taken over.
- **Rows hold IDs, not text.** The message is built from live data at send time, so `payload`
  contains only IDs (a match, a kit, a source) and the log and admin never hold personal content.
- **Dedupe keys name the period.** `instant:<match>:<channel>`, `digest:<period>:<channel>` with the
  period `d:<local date>` or `h:<local date>T<hour>`, `kit_ready:<kit>:<channel>`. A digest period
  with nothing to say is marked used up, so a daily user is not asked again until tomorrow.
- **Time logic is pure.** `schedule.py` has no database: quiet hours (windows may cross midnight),
  when a quiet window ends (UTC), and which digest period is due, from the user's zone, digest time and
  last send. Instant mode also gets the daily digest, for everything below its threshold.
- **Instant alerts** are created from `matching.hooks.after_score`, after the auto-kit is started.
  If a kit is still being built the alert re-checks every ten seconds for up to 90 seconds, then goes
  with or without it (spec §10.4). A kit that finishes later triggers a separate "kit ready" message.
  Quiet hours set `deliver_after`; a task is scheduled for then and the dispatcher is the backstop.
  A match that has an alert waiting or sent is excluded from digests.
- **Failures.** Transient Telegram or SMTP errors retry up to three times with growing delays. If
  the user blocked the bot, their link is deactivated and Telegram is switched off for them.
- **Signed callback data.** Button data is `<action>.<match id>.<12 hex of HMAC-SHA256>` (about 30
  bytes; Telegram allows 64). The HMAC covers the action, the match and the owning user, and the bot
  also looks the chat up to its user and the match up within that user's matches, so a forged button,
  a button forwarded to another chat or an ID guess is refused.
- **`TelegramGateway` interface.** `PtbGateway` (python-telegram-bot, one short event loop per call so
  it works from Huey threads) and `FakeGateway` (tests, `TELEGRAM_GATEWAY=fake`). Telegram is enabled
  when a token is set; without one the feature switches itself off cleanly.
- **Bot behaviour lives in `bot_services.py`**; `bot.py` only hands updates over. Commands are rate
  limited to 30 a minute per chat. Chat IDs are never logged, only a salted hash.
- **Staff notices** (failing sources, new waitlist requests) go to staff users who linked Telegram,
  once per source and state per day. They are queued after commit and can never affect the run or
  the form that triggered them.
- **Telegram URL buttons** are only added when `SITE_URL` is a public https address (Telegram rejects
  localhost); the link is always in the message text as well.
- **Task name:** the spec's `notifications.send_instant(match_id)` is `deliver_notification_task`,
  one task for every kind of notification.

## Consequences

Retries are safe by construction, at the cost of a row per message. Digests can be a few minutes late
(the dispatcher runs every five minutes). "Applied ✓" only sets the match status until Phase 9 creates
the Application. The bot and the web app share the database, so `run_bot` must run next to the worker.
