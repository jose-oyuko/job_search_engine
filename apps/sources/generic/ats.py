"""`ats` source: public job-board APIs of applicant tracking systems (spec §6.4).

Config: `ats` (`greenhouse` or `lever`), `board` (the company's board slug), `company` (display
name). Endpoints, checked against the live public APIs in Phase 4:

- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true`
  returns `{"jobs": [...]}`; each job has `id`, `title`, `absolute_url`, `location.name`,
  `content` (HTML-escaped HTML), `first_published`, `updated_at` and `metadata`.
- Lever: `GET https://api.lever.co/v0/postings/<board>?mode=json` returns a list; each posting has
  `id`, `text`, `hostedUrl`, `applyUrl`, `categories.{location,commitment,allLocations}`,
  `description`, `lists`, `additional`, `workplaceType`, `createdAt` (epoch milliseconds).

Ashby and Workable are not implemented yet (backlog); the adapter table makes adding them a
matter of writing one parse function.
"""

import html
from collections.abc import Callable, Iterator
from typing import Any

from apps.core.sanitize import html_to_text

from ..base import BaseSource, RawJob
from ..exceptions import SourceConfigError, SourceError
from ..registry import register
from .json_api import as_datetime, as_text

_GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
_LEVER_URL = "https://api.lever.co/v0/postings/{board}"


class _Adapter:
    """How to call one ATS and how to turn one of its postings into a `RawJob`."""

    def __init__(
        self,
        url: str,
        params: dict[str, str],
        items: Callable[[Any], list[dict[str, Any]]],
        parse: Callable[[dict[str, Any], str, str], RawJob | None],
        id_of: Callable[[dict[str, Any]], str],
    ) -> None:
        self.url, self.params, self.items, self.parse, self.id_of = url, params, items, parse, id_of


def _greenhouse_items(payload: Any) -> list[dict[str, Any]]:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise SourceError("greenhouse_unexpected_payload")
    return jobs


def _greenhouse_parse(item: dict[str, Any], source: str, company: str) -> RawJob | None:
    title = as_text(item.get("title"))
    if not title:
        return None
    description_html = html.unescape(as_text(item.get("content")))  # Greenhouse escapes its HTML
    workplace = ""
    for entry in item.get("metadata") or []:
        if isinstance(entry, dict) and as_text(entry.get("name")).lower() == "workplace type":
            workplace = as_text(entry.get("value")).lower()
    location = item.get("location")
    raw = {k: v for k, v in item.items() if k != "content"}  # the body is stored once, as HTML
    return RawJob(
        source=source,
        external_id=as_text(item.get("id")),
        url=as_text(item.get("absolute_url")),
        title=title,
        company=as_text(item.get("company_name")) or company,
        apply_url=as_text(item.get("absolute_url")),
        description_html=description_html,
        description_text=html_to_text(description_html),
        locations=[as_text(location.get("name"))]
        if isinstance(location, dict) and location.get("name")
        else [],
        work_type=workplace if workplace in ("remote", "hybrid", "onsite") else "",
        posted_at=as_datetime(item.get("first_published") or item.get("updated_at")),
        raw=raw,
    )


def _lever_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise SourceError("lever_unexpected_payload")
    return payload


def _lever_description(item: dict[str, Any]) -> str:
    parts = [as_text(item.get("description"))]
    for block in item.get("lists") or []:
        if isinstance(block, dict):
            parts.append(
                f"<h3>{html.escape(as_text(block.get('text')))}</h3>"
                f"<ul>{block.get('content', '')}</ul>"
            )
    parts.append(as_text(item.get("additional")))
    return "".join(parts)


def _lever_parse(item: dict[str, Any], source: str, company: str) -> RawJob | None:
    title = as_text(item.get("text"))
    if not title:
        return None
    raw_categories = item.get("categories")
    categories: dict[str, Any] = raw_categories if isinstance(raw_categories, dict) else {}
    locations = categories.get("allLocations") or (
        [categories["location"]] if categories.get("location") else []
    )
    salary = item.get("salaryRange")
    pay_text = ""
    if isinstance(salary, dict) and salary.get("min") is not None:
        pay_text = (
            f"{salary.get('currency', '')} {salary.get('min')}-{salary.get('max')} "
            f"per {as_text(salary.get('interval')).replace('per-', '').replace('-', ' ') or 'year'}"
        ).strip()
    workplace = as_text(item.get("workplaceType")).lower().replace("on-site", "onsite")
    body = _lever_description(item)
    return RawJob(
        source=source,
        external_id=as_text(item.get("id")),
        url=as_text(item.get("hostedUrl")),
        title=title,
        company=company,
        apply_url=as_text(item.get("applyUrl")) or as_text(item.get("hostedUrl")),
        description_html=body,
        description_text=as_text(item.get("descriptionPlain")) or html_to_text(body),
        pay_text=pay_text,
        locations=[as_text(loc) for loc in locations if loc],
        work_type=workplace if workplace in ("remote", "hybrid", "onsite") else "",
        employment_type=as_text(categories.get("commitment")),
        posted_at=as_datetime(item.get("createdAt")),
        raw={k: v for k, v in item.items() if k not in ("description", "lists", "additional")},
    )


_ADAPTERS: dict[str, _Adapter] = {
    "greenhouse": _Adapter(
        _GREENHOUSE_URL, {"content": "true"}, _greenhouse_items, _greenhouse_parse,
        lambda item: as_text(item.get("id")),
    ),
    "lever": _Adapter(
        _LEVER_URL, {"mode": "json"}, _lever_items, _lever_parse,
        lambda item: as_text(item.get("id")),
    ),
}  # fmt: skip


@register("ats")
class AtsSource(BaseSource):
    returns_everything = True

    def _adapter(self) -> _Adapter:
        name = str(self.config.get("ats", ""))
        if name not in _ADAPTERS:
            supported = ", ".join(sorted(_ADAPTERS))
            raise SourceConfigError(f"config.ats must be one of: {supported}")
        if not self.config.get("board"):
            raise SourceConfigError("config.board is required")
        return _ADAPTERS[name]

    def _items(self) -> list[dict[str, Any]]:
        adapter = self._adapter()
        url = adapter.url.format(board=self.config["board"])
        response = self.get(url, params=adapter.params)
        try:
            return adapter.items(response.json())
        except ValueError as exc:
            raise SourceError("invalid_json") from exc

    def list_ids(self) -> list[str]:
        adapter = self._adapter()
        return [i for i in (adapter.id_of(item) for item in self._items()) if i]

    def fetch_all_new(self, seen_ids: set[str]) -> Iterator[RawJob]:
        adapter = self._adapter()
        company = str(self.config.get("company") or self.source.display_name)
        self.ids_seen = []
        for item in self._items():
            external_id = adapter.id_of(item)
            if not external_id:
                continue
            self.ids_seen.append(external_id)
            if self.limit is not None and len(self.ids_seen) > self.limit:
                continue
            job = adapter.parse(item, self.source.name, company)
            if job is None:
                self.errors.append(f"posting_without_title:{external_id}")
                continue
            yield job

    def is_gone(self, external_id: str, url: str) -> bool | None:
        return None
