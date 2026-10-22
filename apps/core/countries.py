"""ISO country and time-zone choices, read from the `tzdata` package (no extra data to maintain)."""

import zoneinfo
from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def country_choices() -> list[tuple[str, str]]:
    """`[("KE", "Kenya"), ...]` sorted by name."""
    text = resources.files("tzdata.zoneinfo").joinpath("iso3166.tab").read_text(encoding="utf-8")
    pairs = [
        (code, name)
        for code, _, name in (line.partition("\t") for line in text.splitlines())
        if code and not code.startswith("#") and len(code) == 2
    ]
    return sorted(pairs, key=lambda pair: pair[1])


@lru_cache(maxsize=1)
def timezone_choices() -> list[tuple[str, str]]:
    """Region/City zones plus UTC, sorted."""
    names = sorted(
        name
        for name in zoneinfo.available_timezones()
        if "/" in name and not name.startswith(("Etc/", "posix/", "right/"))
    )
    return [(name, name.replace("_", " ")) for name in ["UTC", *names]]
