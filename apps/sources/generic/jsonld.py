"""`jsonld` source: detail pages embed schema.org `JobPosting` JSON-LD (spec §6.4).

Config: `list_url`, `link_selector` (CSS, default `a[href]`), `link_pattern` (regex for valid detail
paths), `id_from` (`last_path_segment` or `regex`), `id_regex` (group 1 is the ID, used when
`id_from="regex"`), `max_new_per_run` (default 30).

`find_job_posting` and `posting_to_rawjob` are also used by manual URL input (Phase 5).
"""

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from apps.core.sanitize import html_to_text

from ..base import BaseSource, RawJob
from ..exceptions import SourceConfigError, SourceError
from ..registry import register


def _json_blocks(html: str) -> list[Any]:
    blocks: list[Any] = []
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            blocks.append(json.loads(node.text(strip=True)))
        except ValueError:
            continue  # a broken block must not hide a good one
    return blocks


def _walk(value: Any) -> list[dict[str, Any]]:
    """Flatten lists and `@graph` containers into a list of JSON-LD objects."""
    if isinstance(value, list):
        return [item for entry in value for item in _walk(entry)]
    if isinstance(value, dict):
        found = [value]
        if "@graph" in value:
            found.extend(_walk(value["@graph"]))
        return found
    return []


def find_job_posting(html: str) -> dict[str, Any] | None:
    """The first schema.org JobPosting object in the page's JSON-LD, or None."""
    for block in _json_blocks(html):
        for obj in _walk(block):
            kind = obj.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds:
                return obj
    return None


def _names(value: Any) -> list[str]:
    """Place names from a JSON-LD location value (a Place, Country, address, or a list of them)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [name for item in value for name in _names(item)]
    if isinstance(value, dict):
        if "address" in value:
            return _names(value["address"])
        names: list[str] = []
        for key in ("name", "addressLocality", "addressRegion", "addressCountry"):
            names.extend(_names(value.get(key)))
        return names
    return []


def _salary_text(posting: dict[str, Any]) -> str:
    salary = posting.get("baseSalary")
    if not isinstance(salary, dict):
        return ""
    currency = salary.get("currency", "")
    value = salary.get("value", {})
    if not isinstance(value, dict):
        return f"{currency} {value}".strip()
    low, high, unit = value.get("minValue"), value.get("maxValue"), value.get("unitText", "")
    unit_word = {
        "HOUR": "hour",
        "DAY": "day",
        "WEEK": "week",
        "MONTH": "month",
        "YEAR": "year",
    }.get(str(unit).upper(), "")
    amount = f"{low}-{high}" if low is not None and high is not None else str(low or high or "")
    if not amount:
        return ""
    return f"{currency} {amount}{f' per {unit_word}' if unit_word else ''}".strip()


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def posting_to_rawjob(
    posting: dict[str, Any], *, source: str, external_id: str, url: str
) -> RawJob:
    """Map a schema.org JobPosting to a `RawJob`. Raises SourceError if there is no title."""
    title = str(posting.get("title") or posting.get("name") or "").strip()
    if not title:
        raise SourceError("jobposting_without_title")
    org = posting.get("hiringOrganization")
    company = str(org.get("name", "")) if isinstance(org, dict) else str(org or "")
    description_html = str(posting.get("description") or "")
    locations = _names(posting.get("applicantLocationRequirements")) + _names(
        posting.get("jobLocation")
    )
    work_type = "remote" if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE" else ""
    employment = posting.get("employmentType")
    employment_type = (
        ", ".join(employment) if isinstance(employment, list) else str(employment or "")
    )
    return RawJob(
        source=source,
        external_id=external_id,
        url=url,
        title=title,
        company=company.strip(),
        apply_url=str(posting.get("url") or ""),
        description_html=description_html,
        description_text=html_to_text(description_html),
        pay_text=_salary_text(posting),
        locations=[name for name in locations if name],
        work_type=work_type,
        employment_type=employment_type,
        posted_at=_parse_date(posting.get("datePosted")),
        raw={"jsonld": posting},
    )


@register("jsonld")
class JsonLdSource(BaseSource):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._urls: dict[str, str] = {}

    # --- configuration -------------------------------------------------------------------------

    @property
    def list_url(self) -> str:
        url = str(self.config.get("list_url", ""))
        if not url:
            raise SourceConfigError("config.list_url is required")
        return url

    def external_id_for(self, url: str) -> str | None:
        """The stable ID for a detail URL, or None if it does not look like a job link."""
        path = urlsplit(url).path
        pattern = self.config.get("link_pattern")
        if pattern and not re.search(str(pattern), path):
            return None
        if self.config.get("id_from", "last_path_segment") == "regex":
            match = re.search(str(self.config.get("id_regex", "")), path)
            return match.group(1) if match and match.groups() else None
        segment = path.rstrip("/").rsplit("/", 1)[-1]
        return segment or None

    def detail_url(self, external_id: str) -> str:
        try:
            return self._urls[external_id]
        except KeyError:
            raise SourceError(f"unknown_id:{external_id}") from None

    # --- phase 1: the list ---------------------------------------------------------------------

    def list_ids(self) -> list[str]:
        response = self.get(self.list_url)
        host = urlsplit(self.list_url).netloc.lower()
        selector = str(self.config.get("link_selector", "a[href]"))
        ids: list[str] = []
        for node in HTMLParser(response.text).css(selector):
            href = node.attributes.get("href")
            if not href:
                continue
            absolute = urljoin(self.list_url, href.split("#", 1)[0])
            parts = urlsplit(absolute)
            # Only follow links on the site's own host: a list page must not steer us elsewhere.
            if parts.scheme not in ("http", "https") or parts.netloc.lower() != host:
                continue
            external_id = self.external_id_for(absolute)
            if external_id and external_id not in ids:
                self._urls[external_id] = absolute
                ids.append(external_id)
        return ids

    # --- phase 2: one detail page --------------------------------------------------------------

    def fetch_detail(self, external_id: str) -> RawJob:
        url = self.detail_url(external_id)
        response = self.get(url)
        return self.parse_detail(response.text, external_id, url)

    def parse_detail(self, html: str, external_id: str, url: str) -> RawJob:
        posting = find_job_posting(html)
        if posting is None:
            raise SourceError(f"no_jobposting:{urlsplit(url).path}")
        return posting_to_rawjob(posting, source=self.source.name, external_id=external_id, url=url)
