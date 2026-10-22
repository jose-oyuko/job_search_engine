# 5. Forwarding addresses and one IMAP inbox instead of mailbox OAuth

- Status: Accepted
- Date: 2026-10-21

## Context

Users want LinkedIn and other job-alert emails ingested. Reading each user's mailbox with Gmail or Outlook OAuth brings heavy permissions, verification burdens and privacy risk.

## Decision

Give each user a unique forwarding address on our domain. Cloudflare Email Routing delivers all of them to one dedicated Gmail inbox, which a poller reads with `imap-tools`.

## Consequences

Minimal permissions and no OAuth review process. Users must set up forwarding themselves. Raw email bodies are retained for only 30 days.
