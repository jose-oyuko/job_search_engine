"""`rss` source: RSS 2.0 and Atom feeds (spec §6.4).

Config: `url`, `company_from` (`feed_title` | `item_field` | `fixed`), `company` (used by `fixed`),
`company_field` (child tag name for `item_field`, default `company`), `allow_empty`.

Feeds come from the open internet, so XML is parsed with `defusedxml` (no entity tricks).
"""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import Element  # noqa: S405  (type only; parsing uses defusedxml)

from defusedxml import ElementTree as SafeET

from apps.core.sanitize import clean_html, html_to_text

from ..base import BaseSource, RawJob
from ..exceptions import SourceConfigError, SourceError
from ..registry import register

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_NS = re.compile(r"^\{[^}]+\}")


def _local(tag: str) -> str:
    return _TAG_NS.sub("", tag)


def _child(element: Element, name: str) -> Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element: Element, name: str) -> str:
    child = _child(element, name)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _link(item: Element) -> str:
    link = _child(item, "link")
    if link is None:
        return ""
    return (link.attrib.get("href") or link.text or "").strip()  # Atom uses href=""


@register("rss")
class RssSource(BaseSource):
    returns_everything = True

    def _entries(self) -> tuple[str, list[Element]]:
        url = self.config.get("url")
        if not url:
            raise SourceConfigError("config.url is required")
        response = self.get(str(url))
        try:
            root: Any = SafeET.fromstring(response.content)
        except Exception as exc:  # defusedxml raises several parse/security error types
            raise SourceError(f"invalid_feed:{type(exc).__name__}") from exc
        channel = _child(root, "channel")
        container = channel if channel is not None else root
        feed_title = _text(container, "title")
        entries = [c for c in container if _local(c.tag) in ("item", "entry")]
        return feed_title, entries

    def list_ids(self) -> list[str]:
        return [
            self._external_id(entry) for entry in self._entries()[1] if self._external_id(entry)
        ]

    @staticmethod
    def _external_id(entry: Element) -> str:
        return _text(entry, "guid") or _text(entry, "id") or _link(entry)

    def _company(self, feed_title: str, entry: Element) -> str:
        mode = self.config.get("company_from", "fixed")
        if mode == "feed_title":
            return feed_title
        if mode == "item_field":
            return _text(entry, str(self.config.get("company_field", "company")))
        return str(self.config.get("company", ""))

    def fetch_all_new(self, seen_ids: set[str]) -> Iterator[RawJob]:
        feed_title, entries = self._entries()
        self.ids_seen = []
        for entry in entries:
            external_id = self._external_id(entry)
            title = _text(entry, "title")
            if not external_id:
                continue
            self.ids_seen.append(external_id)
            if self.limit is not None and len(self.ids_seen) > self.limit:
                continue
            if not title:
                self.errors.append(f"item_without_title:{external_id[:60]}")
                continue
            body = _text(entry, "description") or _text(entry, "content") or _text(entry, "summary")
            yield RawJob(
                source=self.source.name,
                external_id=external_id,
                url=_link(entry),
                title=title,
                company=self._company(feed_title, entry),
                description_html=clean_html(body),
                description_text=html_to_text(body),
                posted_at=_parse_date(
                    _text(entry, "pubDate") or _text(entry, "published") or _text(entry, "updated")
                ),
                raw={"title": title, "guid": external_id},
            )

    def is_gone(self, external_id: str, url: str) -> bool | None:
        return None
