# 12. Sources and ingestion: HTML-first ExpertWoka plugin, shared jobs, failure-as-health

- Status: Accepted
- Date: 2026-10-22

## Context

Phase 4 builds the fetcher framework and ingestion (spec §6, §7). Looking at real ExpertWoka pages
showed that the spec's assumption (a `jsonld` source) does not hold: schema.org JobPosting JSON-LD is
present only on jobs restricted to certain countries. Pages open to everyone ("Global") have none. All
of them share the same HTML blocks for title, company, work type, location, compensation, description
and the apply link.

## Decision

- **ExpertWoka is a `custom` plugin** (`apps/sources/custom/expertwoka.py`) built on the generic
  `JsonLdSource` list handling. The detail page is read from HTML first, and JSON-LD only fills gaps
  or rescues a page whose layout changed. If neither works the page is a recorded error. The generic
  `jsonld` kind is still implemented and tested for sites that do embed JSON-LD.
- **Unfiltered listing.** The site has a location filter (`?location=KE`), but jobs are a shared pool
  for all users, so we read the newest list and filter by eligibility ourselves (§7.3).
- **Registry loads lazily** (`registry.load_all()`, called from `AppConfig.ready` and on first lookup)
  instead of importing at package import time, so fetchers can import Django models safely.
- **Two kinds of fetcher.** Page-based sources fetch details only for unseen IDs and `touch_seen`
  the rest; API/RSS/ATS sources return every item each run so the upsert refreshes them. Ingestion
  keeps values the LLM filled in (pay, locations, work type) when a later fetch has none.
- **Failure is health.** Zero job IDs, a fatal listing error, a 429 or all detail pages failing mark
  the run failed; one failure is `degraded`, three in a row `failing`. `allow_empty` opts out for
  sources that can be genuinely empty. A bug in a fetcher is caught, logged and recorded as
  `crashed:<Type>`, never raised into the scheduler.
- **Expiry.** A job not seen for `expire_after_days` (default 7) expires. Before that, the daily task
  re-checks up to 20 jobs per source that dropped off the list for two days and expires those whose
  page returns 404 or 410. Manual and email jobs expire after 45 days.
- **Dedupe** uses `slug(company):slug(title)` (accents, punctuation, company suffixes and the word
  "remote" removed) across different sources within 30 days. Duplicates point at the older job, add
  their URL to `alternate_urls`, and are never extracted, matched or notified. A platform's own link
  replaces an aggregator's as the canonical `apply_url`.
- **Pay.** A bare number is read as a yearly figure above 10,000 only for strong currencies
  (USD, EUR, GBP, CAD, AUD...). `KES 50,000` stays "period unknown" because it is usually monthly.
- **XML is parsed with `defusedxml`**; HTML with `selectolax` (lexbor backend, since 1.0 dropped the
  Modest one). New dependencies: `selectolax`, `defusedxml`, `certifi` (declared explicitly).
- **Extra settings:** `SCRAPER_USER_AGENT` is now wired in, plus `SCRAPER_DELAY_SECONDS` and
  `JOB_EXPIRY_DAYS`.

## Consequences

Adding a normal site is a config row (or one small file). A site that redesigns shows up as a
degraded source on the status page instead of silently going quiet. ExpertWoka's terms allow light
automated access but not republishing, so the owner should ask for permission before a public launch.
