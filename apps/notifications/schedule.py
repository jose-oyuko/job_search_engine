"""Quiet hours and digest due times (spec §13.2, §13.3). Pure functions: no database."""

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Africa/Nairobi"


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def in_quiet_hours(
    local_time: datetime.time, start: datetime.time | None, end: datetime.time | None
) -> bool:
    """Whether `local_time` falls in [start, end). Handles windows that cross midnight; a missing
    or empty window (start == end) means no quiet hours."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def quiet_hours_end(
    now: datetime.datetime,
    tz_name: str,
    start: datetime.time | None,
    end: datetime.time | None,
) -> datetime.datetime | None:
    """When the quiet window we are in ends (UTC), or None if `now` is not in quiet hours."""
    tz = zone(tz_name)
    local = now.astimezone(tz)
    if end is None or not in_quiet_hours(local.time(), start, end):
        return None
    candidate = datetime.datetime.combine(local.date(), end, tzinfo=tz)
    if candidate <= local:
        candidate += datetime.timedelta(days=1)
    return candidate.astimezone(datetime.UTC)


def digest_period(
    mode: str,
    now: datetime.datetime,
    tz_name: str,
    digest_time: datetime.time,
    last_sent: datetime.datetime | None,
    quiet_start: datetime.time | None,
    quiet_end: datetime.time | None,
) -> str | None:
    """A key naming the digest period that is due now, or None if nothing is due.

    - `daily` and `instant` (which sends everything below its threshold in a daily digest): once
      per local day, at or after `digest_time`.
    - `hourly`: at most one an hour, held back during quiet hours.
    - `off`: never.
    The key (`d:2026-03-05`, `h:2026-03-05T14`) is part of the notification's dedupe key, so a
    retry or a second worker can never send the same period twice.
    """
    if mode == "off":
        return None
    tz = zone(tz_name)
    local = now.astimezone(tz)
    if mode == "hourly":
        if in_quiet_hours(local.time(), quiet_start, quiet_end):
            return None
        if last_sent is not None and now - last_sent < datetime.timedelta(hours=1):
            return None
        return f"h:{local:%Y-%m-%dT%H}"
    target = datetime.datetime.combine(local.date(), digest_time, tzinfo=tz)
    if local < target:
        return None
    if last_sent is not None and last_sent.astimezone(tz) >= target:
        return None
    return f"d:{local:%Y-%m-%d}"
