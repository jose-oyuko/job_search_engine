"""`json_api` source: any JSON endpoint (spec §6.4).

Config: `url`, `method` (default GET), `params`, `items_path` (dotted path to the list of items),
`map` (RawJob field -> dotted path inside an item), `id_field` (dotted path to a unique ID),
`company` (fallback employer name), `allow_empty` (an empty list is not a failure).

The list call carries full data, so every item is returned on every run and ingestion upserts.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from ..base import BaseSource, RawJob
from ..exceptions import SourceConfigError, SourceError
from ..registry import register

_TEXT_FIELDS = (
    "title", "company", "url", "apply_url", "description_html", "description_text", "pay_text",
    "work_type", "employment_type",
)  # fmt: skip


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path through dicts and lists (`jobs.0.title`). Missing parts give None."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
        if current is None:
            return None
    return current


def as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def as_locations(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [as_text(v.get("name") if isinstance(v, dict) else v) for v in value if v]
    if isinstance(value, dict):
        return [as_text(value.get("name"))] if value.get("name") else []
    text = as_text(value)
    return [text] if text else []


def as_datetime(value: Any) -> datetime | None:
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value  # epoch milliseconds or seconds
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@register("json_api")
class JsonApiSource(BaseSource):
    returns_everything = True

    def _required(self, key: str) -> Any:
        value = self.config.get(key)
        if not value:
            raise SourceConfigError(f"config.{key} is required")
        return value

    def _items(self) -> list[Any]:
        url = str(self._required("url"))
        response = self.get(
            url, method=str(self.config.get("method", "GET")), params=self.config.get("params")
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("invalid_json") from exc
        items_path = self.config.get("items_path", "")
        items = dig(payload, items_path) if items_path else payload
        if not isinstance(items, list):
            raise SourceError("items_path_not_a_list")
        return items

    def list_ids(self) -> list[str]:
        id_field = str(self._required("id_field"))
        return [
            as_text(dig(item, id_field)) for item in self._items() if as_text(dig(item, id_field))
        ]

    def fetch_all_new(self, seen_ids: set[str]) -> Iterator[RawJob]:
        id_field = str(self._required("id_field"))
        mapping: dict[str, str] = dict(self._required("map"))
        items = self._items()
        self.ids_seen = []
        for item in items:
            external_id = as_text(dig(item, id_field))
            if not external_id:
                continue
            self.ids_seen.append(external_id)
            if self.limit is not None and len(self.ids_seen) > self.limit:
                continue
            values = {name: dig(item, path) for name, path in mapping.items()}
            title = as_text(values.get("title"))
            if not title:
                self.errors.append(f"item_without_title:{external_id}")
                continue
            description_html = as_text(values.get("description_html"))
            yield RawJob(
                source=self.source.name,
                external_id=external_id,
                url=as_text(values.get("url")),
                title=title,
                company=as_text(values.get("company")) or str(self.config.get("company", "")),
                apply_url=as_text(values.get("apply_url")),
                description_html=description_html,
                description_text=as_text(values.get("description_text")),
                pay_text=as_text(values.get("pay_text")),
                locations=as_locations(values.get("locations")),
                work_type=as_text(values.get("work_type")).lower(),
                employment_type=as_text(values.get("employment_type")),
                posted_at=as_datetime(values.get("posted_at")),
                raw=item if isinstance(item, dict) else {"item": item},
            )

    def is_gone(self, external_id: str, url: str) -> bool | None:
        return None  # an item vanishing from the feed is handled by the age-based expiry
