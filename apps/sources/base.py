"""The fetcher contract (spec §6.2, §6.3).

A fetcher's only job is to talk to one site and return standard `RawJob` objects. It does not
dedupe, filter, score or save; ingestion (`apps.jobs.services`) does those things.
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from django.conf import settings

from . import http
from .exceptions import HTTPStatusFailure, SourceError
from .models import Source


@dataclass
class RawJob:
    source: str  # Source.name
    external_id: str  # stable ID within the source (slug, UUID, numeric id)
    url: str  # listing URL
    title: str
    company: str  # the real employer/platform, e.g. "micro1", even if found on an aggregator
    apply_url: str = ""
    description_html: str = ""
    description_text: str = ""
    pay_text: str = ""  # raw, e.g. "$50 - $80/hr" or "PPT Rate: $60 per task"
    locations: list[str] = field(default_factory=list)  # raw: "Global", "Kenya", "KE", "EMEA"...
    work_type: str = ""  # remote / onsite / hybrid / "" if unknown
    employment_type: str = ""
    posted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # original payload for re-parsing


class BaseSource:
    """Two-phase fetching: a cheap list request, then detail requests for unseen IDs only.

    Sources whose list call already carries full data (APIs, RSS) override `fetch_all_new` and
    return every item, so the upsert in ingestion keeps them fresh.
    """

    kind = ""
    # True when `fetch_all_new` returns every item each run (so ingestion marks them seen).
    returns_everything = False

    def __init__(
        self,
        source: Source,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        limit: int | None = None,
    ) -> None:
        self.source = source
        self.client = client
        self.config: dict[str, Any] = source.config or {}
        self._sleep = sleep
        self.limit = limit  # caps new jobs per run (test_source --limit)
        self.ids_seen: list[str] = []
        self.errors: list[str] = []

    # --- helpers -----------------------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return http.fetch(self.client, url, sleep=self._sleep, **kwargs)

    def pause(self) -> None:
        """Be polite between requests to the same host (spec §6.3)."""
        delay = float(self.config.get("delay_seconds", settings.SCRAPER_DELAY_SECONDS))
        if delay > 0:
            self._sleep(delay)

    @property
    def max_new(self) -> int:
        configured = int(self.config.get("max_new_per_run", 30))
        return min(configured, self.limit) if self.limit is not None else configured

    # --- the contract ------------------------------------------------------------------------

    def list_ids(self) -> list[str]:
        """Phase 1: one request for the newest-first listing, returning external IDs."""
        raise NotImplementedError

    def fetch_detail(self, external_id: str) -> RawJob:
        """Phase 2: fetch and parse one job. Only called for IDs we have not seen."""
        raise NotImplementedError

    def fetch_all_new(self, seen_ids: set[str]) -> Iterator[RawJob]:
        """Default orchestration: list, then fetch details for unseen IDs, newest first.

        A failure on one detail page is recorded in `self.errors` and does not stop the run.
        """
        ids = self.list_ids()
        self.ids_seen = ids
        new_ids = [i for i in ids if i not in seen_ids][: self.max_new]
        for position, external_id in enumerate(new_ids):
            if position:
                self.pause()
            try:
                yield self.fetch_detail(external_id)
            except SourceError as exc:
                self.errors.append(str(exc))

    def match_url(self, url: str) -> str | None:
        """If `url` is a detail page of this source, its external ID; otherwise None. Used by
        manual "paste a URL" input to reuse the source's own parser (spec §8.1)."""
        return None

    def is_gone(self, external_id: str, url: str) -> bool | None:
        """Re-check one job: True if its page is gone (404/410), False if it is still there, None
        if we cannot tell. Used by the daily expiry task. Page-based sources just re-request the
        page; API sources override this to return None."""
        if not url:
            return None
        try:
            self.get(url)
        except HTTPStatusFailure as exc:
            return exc.status in (404, 410)
        except SourceError:
            return None
        return False
