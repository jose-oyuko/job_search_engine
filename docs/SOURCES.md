# Adding a job source

A *source* is one place jobs come from. Fetchers only talk to a site and return standard `RawJob`
objects; ingestion (`apps/jobs/services.py`) does everything else (sanitising, pay and eligibility
parsing, dedupe, scam flags, expiry, LLM extraction). You never write to the database from a fetcher.

## Decision tree

1. **Does the job page embed schema.org `JobPosting` JSON-LD?** Use `jsonld`.
2. **Is the company on a known applicant tracking system?** Use `ats` (Greenhouse and Lever today).
3. **Is there a JSON request in the browser DevTools Network tab?** Use `json_api`.
4. **Is there an RSS or Atom feed?** Use `rss`.
5. **Is it low volume and unstructured?** Use `llm_extract` (costs LLM money per new page).
6. **Otherwise** write a `custom` plugin (see ExpertWoka for an example).

Create the source in the admin (Sources → Add) with a unique `name`, the `kind`, and a `config` JSON.
Then try it:

```bash
uv run python manage.py test_source <name> --dry-run --limit 5     # fetch and print, save nothing
uv run python manage.py test_source <name> --limit 5               # a real run (saves jobs)
```

`test_source` is one of the only two places that touch job sites for real (the other is the running
worker). Tests never do; they use saved pages and `respx`.

## Config per kind

| kind | keys |
|---|---|
| `jsonld` | `list_url`, `link_selector` (CSS, default `a[href]`), `link_pattern` (regex on the link **path**), `id_from` (`last_path_segment` or `regex`), `id_regex` (group 1 is the ID), `max_new_per_run` (30) |
| `ats` | `ats` (`greenhouse` or `lever`), `board` (company slug), `company` (display name) |
| `json_api` | `url`, `method`, `params`, `items_path` (dotted), `map` (RawJob field to dotted path), `id_field`, `company`, `allow_empty` |
| `rss` | `url`, `company_from` (`feed_title`, `item_field` or `fixed`), `company`, `company_field`, `allow_empty` |
| `llm_extract` | `list_url`, `link_selector`, `link_pattern`, `company`, `max_new_per_run` (10) |
| `custom` | whatever the plugin documents; the plugin is looked up by the source's `name` |

Every kind also accepts `delay_seconds` (pause between requests, default `SCRAPER_DELAY_SECONDS` =
1.5), `expire_after_days` (default `JOB_EXPIRY_DAYS` = 7) and `max_new_per_run`.

## How a run works

1. The scheduler (`sources.tick`, every 5 minutes) queues `run_source` for each enabled source whose
   `interval_minutes` has passed. A per-source lock stops overlapping runs.
2. **Two-phase sources** (`jsonld`, `llm_extract`, ExpertWoka) request the listing once, then fetch a
   detail page only for IDs that are not in the database yet (newest first, at most
   `max_new_per_run`, pausing between requests). Jobs still listed are marked seen without
   re-fetching.
3. **List-complete sources** (`ats`, `json_api`, `rss`) get every item from the list call and
   upsert them all, so changes are picked up.
4. Ingestion creates or updates jobs, links cross-source duplicates and records counts on a `SourceRun`.
5. New canonical jobs are queued for LLM extraction (`extract_job`, system-paid, once per job).
6. Health: one failed run makes the source **degraded**, three in a row make it **failing**, a
   success resets it. "No job IDs found" counts as a failure (that is how a changed site shows up);
   set `allow_empty` for sources that can legitimately be empty. Failing sources are logged now and
   will notify staff on Telegram in Phase 8.

## Writing a plugin

Subclass `BaseSource` (or `JsonLdSource` if the site is link-list-then-detail-page), register it and
implement `list_ids` and `fetch_detail` (or `parse_detail`):

```python
# apps/sources/custom/mysite.py
from ..generic.jsonld import JsonLdSource
from ..registry import register_custom


@register_custom("mysite")  # matches Source.name
class MySiteSource(JsonLdSource):
    def parse_detail(self, html, external_id, url): ...  # return a RawJob
```

Rules for fetchers: use `self.get()` (it retries, honours 429 and logs the path only), call
`self.pause()` between detail requests, never follow links to other hosts, raise `SourceError` with a
short code (no page content) when a page cannot be parsed, and record per-item problems in
`self.errors` instead of raising so one bad page does not stop the run.

Add a test with a **saved page** in `tests/fixtures/html/<source>/` and `respx` for HTTP. Save pages
once, by hand, and be polite when you do.

## ExpertWoka notes

- Config: unfiltered newest list, page 1, at most 30 detail pages per run, 1.5 s between requests.
- JSON-LD exists only on location-restricted jobs, so the plugin reads the HTML blocks
  (title, company, work type, location, compensation, description, apply card) and uses JSON-LD only
  as a fallback.
- The site's own location filter is `?location=<ISO code | region | GLOBAL>`; we do not use it
  because jobs are shared by all users and filtered by eligibility on our side.
- `apply_url` is whatever the apply button links to; it may be a platform referral link.
- robots.txt does not exist (404). The terms (checked in Phase 4) forbid using automated systems to
  "overload, disrupt or unlawfully extract" information and forbid republishing or commercially
  exploiting their materials without permission. Our polling is light and we link back to every
  listing; ask ExpertWoka for permission before offering the product publicly.
