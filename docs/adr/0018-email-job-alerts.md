# 18. Email job alerts: forwarding addresses, parsed from the body, system-paid reading

- Status: Accepted
- Date: 2026-10-27

## Context

Phase 10 lets users forward job-alert emails (LinkedIn and others) into their feed (spec §9). LinkedIn
cannot be scraped, and users' mailbox passwords or OAuth tokens must never be held.

## Decision

- **Forwarding addresses, not mailbox access.** Each user has an `InboundAddress` (`u-` plus six
  look-alike-free characters). All addresses on `INBOUND_EMAIL_DOMAIN` land in one dedicated inbox
  that one task reads over IMAP every 5 minutes. Existing users get an address by a data migration,
  new users at signup, and the settings page creates it on first visit if it is missing.
- **The mailbox is behind `MailboxClient`.** `ImapMailbox` (imap-tools) and `FakeMailbox` (saved
  `.eml` files). Messages are parsed with the standard library into one `MailMessage` for both. A
  message is marked read and moved only after it has been handled, to `jobpilot/processed` or
  `jobpilot/unrouted`; a failure is recorded and the message is still filed, so one bad email is not
  retried forever and cannot block the rest.
- **Routing by headers.** The address may be in To, Delivered-To, X-Forwarded-To, X-Original-To,
  Envelope-To or Cc; the domain must be ours. Unknown tokens, foreign domains and inactive users or
  addresses are "unrouted": not recorded (there is no user to own the row), only counted and filed.
- **De-duplication by `Message-ID`** (unique in `InboundEmail`); a message without one gets a hash of
  sender, subject and date. A replay is filed as processed and does nothing. Jobs are de-duplicated
  on `(origin, external_id)` for email jobs, then by URL and company and title like any manual job.
- **LinkedIn is read from the email only.** The parser recognises `/jobs/view/<id>` links, takes the
  title from the link text and the company and location from the lines of the smallest block around
  it that belongs to that job alone, and builds the canonical `https://www.linkedin.com/jobs/view/<id>`
  URL. It never fetches LinkedIn. A text-only layout is handled too. Without real samples the
  heuristics are tested on fixtures that mimic both layouts; `docs/EMAIL_SETUP.md` asks for real
  `.eml` files to tune them.
- **Other senders:** links to a configured source's detail pages go through that source's own parser
  (at most five fetches per email, through the normal source client). Otherwise one model call lists
  the jobs (`extract_email_jobs`), at most 15, **system-paid** (`user=None`, under the daily system
  cap), with the email wrapped as untrusted data. The model may only return links that are present in
  the email; any other link is discarded. A model failure yields no jobs, never an error.
- **Matching:** the receiving user's match is created first and forced (they forwarded it), then
  everyone's rule filters see the job through `match_new_job_task`. Email jobs carry little text, so
  extraction is skipped unless there is a real description. 
- **Forwarded jobs are never taken away by the rule filter.** `Match.user_added` records that the
  user put the job in front of the rules (added by hand or by email). `upsert_match` keeps such a
  match even when the job does not fit their targets. This also fixes a bug found while testing:
  before, the batch run after extraction, and a re-match, deleted a manually added job that was
  outside the user's target. A data migration marks existing matches that failed the rules.
- **Gmail forwarding confirmation:** recognised by sender `forwarding-noreply@google.com` plus the
  subject. Only an https link on `mail-settings.google.com` is stored; any other link (the email is
  untrusted) is ignored, so a forged "confirmation" cannot put an arbitrary address on a banner. The
  banner appears on the dashboard and the settings page, and Telegram users get one notice.
- **Retention:** a daily task clears `body_text` and `body_html` after 30 days; metadata stays.
- **Logging:** counts, parser names and IDs only. Addresses, tokens, senders, subjects and bodies are
  never logged.

## Consequences

LinkedIn jobs arrive with no description, so scores rely on title, company and location. Parsing
quality for real LinkedIn emails is unproven until a real sample is run. The first run on a mailbox
creates two folders. The address is a bearer token: anyone who knows it can send mail to that user's
feed (the content is still untrusted and parsed as data), which is why it is not shown anywhere but
the owner's own settings page.
