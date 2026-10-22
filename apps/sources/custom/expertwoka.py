"""ExpertWoka (https://expertwoka.com): an aggregator of AI-training and remote roles (spec §6.5).

What the live site actually looks like (checked in Phase 4 against saved pages in
`tests/fixtures/html/expertwoka/`):

- The listing `https://expertwoka.com/opportunities?sort=newest&page=1` links each job as
  `/opportunities/<slug>` or `/opportunities/sourcing/<id>` (ExpertWoka's own roles).
- Every detail page has the same HTML blocks: `h1.ew-job-title`, `a.ew-job-company-link`,
  `.ew-job-posted`, three `.ew-job-stat` blocks (Work Type, Location, Compensation),
  `.ew-job-description`, `.ew-job-tag` skills and an apply card.
- schema.org JobPosting JSON-LD is present **only on location-restricted jobs**; pages that are open
  to everyone ("Global") have none. So the HTML is the primary source and JSON-LD only fills gaps.
- The site has a location filter (`?location=KE`, also region codes and `GLOBAL`), but jobs are
  shared across all users, so we read the unfiltered newest list and filter by eligibility
  ourselves.

Politeness (spec §6.5): list page 1 only, at most 30 detail fetches per run, 1-2 s between requests.
"""

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from apps.core.sanitize import html_to_text

from .. import generic  # noqa: F401  (ensures the jsonld base class is importable first)
from ..base import RawJob
from ..exceptions import SourceError
from ..generic.jsonld import JsonLdSource, find_job_posting, posting_to_rawjob
from ..registry import register_custom

BASE = "https://expertwoka.com"
PATH_PREFIX = "/opportunities/"
DEFAULTS: dict[str, Any] = {
    "list_url": f"{BASE}/opportunities?sort=newest&page=1",
    "link_selector": "a[href*='/opportunities/']",
    "link_pattern": r"^/opportunities/(?:sourcing/\d+|[a-z0-9][a-z0-9-]*)$",
    "max_new_per_run": 30,
}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NOT_SPECIFIED = {"", "not specified", "n/a", "tbd"}
_WORK_TYPES = {"remote": "remote", "on-site": "onsite", "onsite": "onsite", "hybrid": "hybrid"}
_CONFIDENTIAL = {"confidential client", "confidential"}


def _text(node: Node | None) -> str:
    return re.sub(r"\s+", " ", node.text(strip=True)).strip() if node is not None else ""


@register_custom("expertwoka")
class ExpertWokaSource(JsonLdSource):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.config = {**DEFAULTS, **self.config}

    def external_id_for(self, url: str) -> str | None:
        path = urlsplit(url).path
        if not re.search(str(self.config["link_pattern"]), path):
            return None
        return path.removeprefix(PATH_PREFIX).rstrip("/") or None

    def detail_url(self, external_id: str) -> str:
        return self._urls.get(external_id) or urljoin(BASE, f"{PATH_PREFIX}{external_id}")

    def parse_detail(self, html: str, external_id: str, url: str) -> RawJob:
        tree = HTMLParser(html)
        posting = find_job_posting(html)
        title = _text(tree.css_first("h1.ew-job-title"))
        if not title:
            if posting is not None:  # the layout changed but JSON-LD survived: use it
                return posting_to_rawjob(
                    posting, source=self.source.name, external_id=external_id, url=url
                )
            raise SourceError(f"unparseable_detail:{urlsplit(url).path}")

        stats = self._stats(tree)
        company = self._company(tree, external_id, posting)
        description = tree.css_first(".ew-job-description")
        description_html = (description.inner_html or "").strip() if description is not None else ""
        pay_text = stats.get("compensation", "")
        location = stats.get("location", "")
        return RawJob(
            source=self.source.name,
            external_id=external_id,
            url=url,
            title=title,
            company=company,
            apply_url=self._apply_url(tree),
            description_html=description_html,
            description_text=html_to_text(description_html),
            pay_text="" if pay_text.lower() in _NOT_SPECIFIED else pay_text,
            locations=[location] if location else self._posting_locations(posting),
            work_type=_WORK_TYPES.get(stats.get("work type", "").lower(), ""),
            posted_at=self._posted_at(tree, posting),
            raw={
                "stats": stats,
                "skills": [_text(tag) for tag in tree.css(".ew-job-tag")],
                "jsonld": posting,
            },
        )

    # --- pieces ----------------------------------------------------------------------------------

    @staticmethod
    def _stats(tree: HTMLParser) -> dict[str, str]:
        stats: dict[str, str] = {}
        for block in tree.css(".ew-job-stat"):
            label = _text(block.css_first(".ew-job-stat-label")).lower()
            value = _text(block.css_first(".ew-job-stat-value"))
            if label:
                stats[label] = value
        return stats

    @staticmethod
    def _company(tree: HTMLParser, external_id: str, posting: dict[str, Any] | None) -> str:
        if external_id.startswith("sourcing/"):
            return "ExpertWoka Sourcing"  # the real client is confidential
        name = _text(tree.css_first("a.ew-job-company-link"))
        if name:
            return name
        org = posting.get("hiringOrganization") if posting else None
        candidate = str(org.get("name", "")) if isinstance(org, dict) else ""
        return "" if candidate.lower() in _CONFIDENTIAL else candidate

    @staticmethod
    def _apply_url(tree: HTMLParser) -> str:
        """The direct application link (platform or referral link) in the apply card."""
        card = tree.css_first("#opportunity-application")
        if card is None:
            return ""
        for link in card.css("a[href]"):
            href = link.attributes.get("href") or ""
            if href.startswith(("http://", "https://")) and "apply" in _text(link).lower():
                return href
        return ""

    @staticmethod
    def _posting_locations(posting: dict[str, Any] | None) -> list[str]:
        if not posting:
            return []
        parsed = (
            posting_to_rawjob(posting, source="", external_id="", url="")
            if posting.get("title")
            else None
        )
        return parsed.locations if parsed else []

    @staticmethod
    def _posted_at(tree: HTMLParser, posting: dict[str, Any] | None) -> datetime | None:
        match = _DATE_RE.search(_text(tree.css_first(".ew-job-posted")))
        raw = match.group(0) if match else (str(posting.get("datePosted", "")) if posting else "")
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=UTC) if raw else None
        except ValueError:
            return None
