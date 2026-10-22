"""Location and eligibility normalisation (spec §7.3).

Turns raw location strings ("Global", "Kenya", "US only", "EMEA", "Nairobi, KE") into ISO-2
country codes and region codes, and answers "may a user in country X apply?". Country names come
from the `tzdata` ISO table (see `apps.core.countries`); regions are defined here.

Unknown eligibility is never treated as "no": `is_eligible` returns None and the UI shows an
"eligibility unclear" badge instead of hiding the job.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from apps.core.countries import country_choices

GLOBAL = "GLOBAL"

_AFRICA = [
    "DZ",
    "AO",
    "BJ",
    "BW",
    "BF",
    "BI",
    "CV",
    "CM",
    "CF",
    "TD",
    "KM",
    "CG",
    "CD",
    "CI",
    "DJ",
    "EG",
    "GQ",
    "ER",
    "SZ",
    "ET",
    "GA",
    "GM",
    "GH",
    "GN",
    "GW",
    "KE",
    "LS",
    "LR",
    "LY",
    "MG",
    "MW",
    "ML",
    "MR",
    "MU",
    "MA",
    "MZ",
    "NA",
    "NE",
    "NG",
    "RW",
    "ST",
    "SN",
    "SC",
    "SL",
    "SO",
    "ZA",
    "SS",
    "SD",
    "TZ",
    "TG",
    "TN",
    "UG",
    "ZM",
    "ZW",
]
_EU = [
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
]
_EUROPE_EXTRA = [
    "GB",
    "CH",
    "NO",
    "IS",
    "LI",
    "AL",
    "AD",
    "BA",
    "BY",
    "MD",
    "MC",
    "ME",
    "MK",
    "RS",
    "SM",
    "UA",
    "VA",
    "RU",
]
_MIDDLE_EAST = [
    "AE",
    "BH",
    "IR",
    "IQ",
    "IL",
    "JO",
    "KW",
    "LB",
    "OM",
    "PS",
    "QA",
    "SA",
    "SY",
    "TR",
    "YE",
]
_MENA = [
    "DZ",
    "BH",
    "EG",
    "IR",
    "IQ",
    "IL",
    "JO",
    "KW",
    "LB",
    "LY",
    "MA",
    "OM",
    "PS",
    "QA",
    "SA",
    "SY",
    "TN",
    "AE",
    "YE",
    "MR",
    "DJ",
]
_NA = ["US", "CA", "MX"]
_LATAM = [
    "AR",
    "BO",
    "BR",
    "CL",
    "CO",
    "CR",
    "CU",
    "DO",
    "EC",
    "SV",
    "GT",
    "HN",
    "HT",
    "MX",
    "NI",
    "PA",
    "PY",
    "PE",
    "UY",
    "VE",
    "PR",
    "BZ",
]
_APAC = [
    "AF",
    "BD",
    "BT",
    "BN",
    "KH",
    "CN",
    "HK",
    "IN",
    "ID",
    "JP",
    "KZ",
    "KG",
    "LA",
    "MO",
    "MY",
    "MV",
    "MN",
    "MM",
    "NP",
    "KP",
    "PK",
    "PH",
    "SG",
    "KR",
    "LK",
    "TW",
    "TJ",
    "TH",
    "TL",
    "TM",
    "UZ",
    "VN",
    "AU",
    "NZ",
    "FJ",
    "PG",
    "WS",
    "TO",
    "VU",
    "SB",
    "FM",
    "KI",
    "MH",
    "NR",
    "PW",
    "TV",
]

REGION_COUNTRIES: dict[str, frozenset[str]] = {
    "AFRICA": frozenset(_AFRICA),
    "EU": frozenset(_EU),
    "EUROPE": frozenset(_EU + _EUROPE_EXTRA),
    "EMEA": frozenset(_EU + _EUROPE_EXTRA + _AFRICA + _MIDDLE_EAST),
    "NA": frozenset(_NA),
    "LATAM": frozenset(_LATAM),
    "APAC": frozenset(_APAC),
    "MENA": frozenset(_MENA),
}
REGION_CODES = frozenset(REGION_COUNTRIES)

# Region names and spellings. Matched as whole words on lowercase text.
_REGION_ALIASES: dict[str, str] = {
    "africa": "AFRICA",
    "sub-saharan africa": "AFRICA",
    "sub saharan africa": "AFRICA",
    "emea": "EMEA",
    "europe": "EUROPE",
    "european union": "EU",
    "eu": "EU",
    "north america": "NA",
    "north_america": "NA",
    "latin america": "LATAM",
    "latam": "LATAM",
    "south america": "LATAM",
    "apac": "APAC",
    "asia-pacific": "APAC",
    "asia pacific": "APAC",
    "asia": "APAC",
    "middle east": "MENA",
    "mena": "MENA",
    "middle east and north africa": "MENA",
}
_GLOBAL_WORDS = (
    "global",
    "globally",
    "worldwide",
    "world wide",
    "anywhere",
    "anywhere in the world",
    "all countries",
    "international",
)

_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states of america": "US",
    "america": "US",
    "uk": "GB",
    "united kingdom": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "uae": "AE",
    "south korea": "KR",
    "korea": "KR",
    "russia": "RU",
    "czech republic": "CZ",
    "czechia": "CZ",
    "ivory coast": "CI",
    "cote d'ivoire": "CI",
    "côte d'ivoire": "CI",
    "dr congo": "CD",
    "drc": "CD",
    "democratic republic of the congo": "CD",
    "republic of the congo": "CG",
    "eswatini": "SZ",
    "swaziland": "SZ",
    "cape verde": "CV",
    "turkey": "TR",
    "türkiye": "TR",
    "vietnam": "VN",
    "hong kong": "HK",
    "taiwan": "TW",
    "the netherlands": "NL",
    "holland": "NL",
    "the philippines": "PH",
    "the gambia": "GM",
    "burma": "MM",
}

_REGION_ORDER = sorted(REGION_COUNTRIES)


@dataclass(frozen=True)
class Eligibility:
    countries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    is_global: bool = False
    known: bool = False


class _Located(Protocol):
    eligible_countries: list[str]
    eligible_regions: list[str]
    is_global: bool
    eligibility_known: bool


@lru_cache(maxsize=1)
def _name_to_code() -> dict[str, str]:
    """Lowercase country names and aliases -> ISO-2, plus parenthesised short forms."""
    table: dict[str, str] = {}
    for code, name in country_choices():
        lowered = name.lower()
        table[lowered] = code
        # The ISO table writes "Korea (South)", "Congo (Dem. Rep.)", "Macau"...
        base = re.sub(r"\s*\(.*?\)", "", lowered).strip()
        table.setdefault(base, code)
    for name, code in list(table.items()):
        if "&" in name:
            table.setdefault(name.replace("&", "and"), code)  # "Trinidad & Tobago"
    table.update(_COUNTRY_ALIASES)
    # Short names the ISO table spells differently or omits.
    table.setdefault("united states", "US")
    return table


@lru_cache(maxsize=1)
def _phrase_pattern() -> re.Pattern[str]:
    """One alternation of every known phrase, longest first, so "south africa" beats "africa"."""
    phrases = set(_name_to_code()) | set(_REGION_ALIASES) | set(_GLOBAL_WORDS)
    ordered = sorted(phrases, key=len, reverse=True)
    escaped = "|".join(re.escape(p) for p in ordered)
    return re.compile(rf"(?<![\w-])(?:{escaped})(?![\w-])", re.IGNORECASE)


_CODE_RE = re.compile(r"^[A-Z]{2}$")
_EMBEDDED_CODE_RE = re.compile(r"\b[A-Z]{2}\b")
# Upper-case English words that are also country codes. Inside a longer string they are read as
# words ("Remote - IT support"); on their own as a whole token they are read as codes.
_AMBIGUOUS_CODES = frozenset(
    [
        "AS",
        "AT",
        "BE",
        "BY",
        "DO",
        "ID",
        "IN",
        "IS",
        "IT",
        "ME",
        "NO",
        "OR",
        "SO",
        "TO",
        "AM",
        "AD",
        "MY",
    ]
)


@lru_cache(maxsize=1)
def _codes() -> frozenset[str]:
    return frozenset(code for code, _ in country_choices())


_SPLIT_RE = re.compile(r"[;,/|\n]+|\s+[-–—]\s+|\(|\)")
_ONLY_RE = re.compile(r"\b(?:only|based|resident|residents|citizens?|eligible|candidates?)\b", re.I)


def parse_locations(raw_locations: Iterable[str]) -> Eligibility:
    """Normalise raw location strings. `known=False` when nothing could be recognised."""
    countries: list[str] = []
    regions: list[str] = []
    is_global = False
    names = _name_to_code()

    def add(target: list[str], value: str) -> None:
        if value not in target:
            target.append(value)

    for raw in raw_locations:
        text = (raw or "").strip()
        if not text:
            continue
        # A bare upper-case code such as "KE" (or "GLOBAL"): exact tokens only, so that the
        # English words "in", "it" or "us" are never mistaken for countries.
        for token in _SPLIT_RE.split(text):
            token = token.strip()
            if _CODE_RE.match(token) and token in _codes():
                add(countries, token)
            elif token.upper() == GLOBAL:
                is_global = True
            elif token.upper() in REGION_CODES:
                add(regions, token.upper())
            elif token.upper() == "NORTH_AMERICA":
                add(regions, "NA")
        for word in _EMBEDDED_CODE_RE.findall(text):  # "US only", "Nairobi KE"
            if word in _codes() and word not in _AMBIGUOUS_CODES:
                add(countries, word)
        cleaned = _ONLY_RE.sub(" ", text)
        for match in _phrase_pattern().finditer(cleaned):
            phrase = match.group(0).lower()
            if phrase in _GLOBAL_WORDS:
                is_global = True
            elif phrase in _REGION_ALIASES:
                add(regions, _REGION_ALIASES[phrase])
            elif phrase in names:
                add(countries, names[phrase])
    known = is_global or bool(countries) or bool(regions)
    return Eligibility(countries=countries, regions=regions, is_global=is_global, known=known)


def is_eligible(job: _Located, country: str) -> bool | None:
    """May someone in `country` (ISO-2) apply? `None` means the job's eligibility is unknown."""
    if not job.eligibility_known:
        return None
    country = country.upper()
    if job.is_global:
        return True
    if country in job.eligible_countries:
        return True
    for region in job.eligible_regions:
        if country in REGION_COUNTRIES.get(region, frozenset()):
            return True
    if job.eligible_countries or job.eligible_regions:
        return False
    return None
