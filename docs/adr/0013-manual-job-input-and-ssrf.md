# 13. Manual job input: one SSRF-safe fetcher, source reuse first, private text jobs

- Status: Accepted
- Date: 2026-10-22

## Context

Users can add a job by pasting a link or a description (spec §8). Fetching a link a user typed is
the riskiest thing the app does: the URL could point at our own database, the cloud metadata service
or an internal page. The spec also wants the paths tried in a fixed order and wants pasted text kept
private.

## Decision

- **All user URLs go through `apps/core/ssrf.py`.** It allows only http(s) on ports 80 and 443 with
  no credentials, resolves the name, and requires *every* answer to be a public address (private,
  loopback, link-local, carrier-grade NAT, multicast, reserved and IPv4-mapped-private are refused).
  Odd IPv4 spellings (`2130706433`, `0x7f.1`, `0177.0.0.1`, `127.1`) are normalised by our own code
  instead of a DNS query.
- **The connection is pinned to the validated address.** The request goes to the IP, with the
  original host in the `Host` header and as the TLS server name, so DNS rebinding between the check
  and the connection cannot redirect us. Redirects are followed by hand (at most 5) and every hop is
  resolved and validated again. The response is capped at 2 MB and 20 seconds and must look like a
  web page.
- **Blocked sites are refused before any request**, on the typed address and on every redirect hop
  (a short link that bounces to LinkedIn never contacts LinkedIn).
- **Path order:** blocked sites, then a configured source's own parser (a URL on a source's host that
  matches its job-link pattern, so an ExpertWoka URL creates a normal public scraped job with no
  user-paid LLM call), then the page's JSON-LD (no LLM), then `extract_page` on the user's
  `monthly_manual_extractions`. `llm_extract` sources opt out of URL matching so their pages are read
  on the user's allowance, not the system's.
- **Pasted text** creates a private job owned by the user through `extract_job`. `JobExtraction` gained
  optional `title` and `company` so a single call can name the role; if the text names neither and the
  user gave none, the form asks. The extraction cache key includes the user, so private text never
  crosses users, and the same text pasted twice by one user returns the same job at no charge.
- **Dedupe** applies to public URL jobs only (same canonical URL, or same company and title within 30
  days). Private jobs are never matched against anyone else's.
- **Limits:** 20 additions per user per day, one shared counter for both tabs (django-ratelimit).
- **Matching hook:** `apps/jobs/matching_hook.py` calls `apps.matching.services.match_job_for_user`
  once it exists (Phase 6) and does nothing before then.
- **`log_event` hardening:** a field named like a `LogRecord` attribute (`created`, `name`, `module`)
  now gets a `_field` suffix instead of raising `KeyError`. Found while testing this phase.

## Consequences

One reviewed code path handles every user-supplied URL, and its guarantees are tested without any
network. The blocked-domain list is a plain tuple in `apps/jobs/manual.py`. Only ports 80 and 443 are
allowed, which excludes the occasional site on a custom port.
