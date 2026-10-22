"""Pay normalisation (spec §7.2): free-form pay text -> min, max, currency and period.

Pure logic, no Django imports. Anything it cannot read confidently is returned as "unknown"
rather than guessed, and the raw text is always kept on the job by the caller.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

UNKNOWN = "unknown"

# Currency markers, longest first so "ksh" wins over "k".
_CURRENCIES: dict[str, str] = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "kes": "KES",
    "kshs": "KES",
    "ksh": "KES",
    "kes.": "KES",
    "ngn": "NGN",
    "₦": "NGN",
    "zar": "ZAR",
    "ugx": "UGX",
    "tzs": "TZS",
    "inr": "INR",
    "₹": "INR",
    "cad": "CAD",
    "aud": "AUD",
    "php": "PHP",
    "ghs": "GHS",
}
_CURRENCY_ALT = "|".join(re.escape(c) for c in sorted(_CURRENCIES, key=len, reverse=True))

# Currencies where a bare large number does not imply an annual salary (KES 50,000 is a month).
_WEAK_CURRENCIES = {"KES", "NGN", "UGX", "TZS", "INR", "PHP", "GHS", "ZAR"}

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_AMOUNT_RE = re.compile(
    rf"(?P<cur>{_CURRENCY_ALT})?\s*(?P<num>{_NUMBER})\s*(?P<k>k\b)?",
    re.IGNORECASE,
)
_RANGE_SEP_RE = re.compile(r"\s*(?:-|–|—|to)\s*", re.IGNORECASE)

_UP_TO_RE = re.compile(
    r"\b(up\s*to|max(?:imum)?(?:\s+rate)?|at\s+most|capped\s+at|not\s+more\s+than)\b", re.I
)
_FROM_RE = re.compile(
    r"\b(from|starting\s+(?:at|from)|min(?:imum)?(?:\s+rate)?|at\s+least|\w*\+)", re.I
)

_PERIODS: list[tuple[str, re.Pattern[str]]] = [
    ("one_time", re.compile(r"one[\s-]?time|lump[\s-]?sum|flat\s+fee|fixed\s+fee", re.I)),
    ("task", re.compile(r"\bper\s+task\b|/\s*task\b|\bppt\b|\bpertask\b", re.I)),
    (
        "hour",
        re.compile(
            r"/\s*h(?:r|our)s?\b|\bper\s+hour\b|\bhourly\b|\ban\s+hour\b|\bph\b|/\s*hr\b", re.I
        ),
    ),
    ("day", re.compile(r"/\s*day\b|\bper\s+day\b|\bdaily\b|\ba\s+day\b", re.I)),
    ("week", re.compile(r"/\s*(?:wk|week)\b|\bper\s+week\b|\bweekly\b|\ba\s+week\b", re.I)),
    (
        "month",
        re.compile(
            r"/\s*(?:mo|month)\b|\bper\s+month\b|\bmonthly\b|\ba\s+month\b|\bpm\b|\bp\.m\.", re.I
        ),
    ),
    (
        "year",
        re.compile(
            r"/\s*(?:yr|year)\b|\bper\s+(?:year|annum)\b|\byearly\b|\bannual(?:ly)?\b|\bp\.?a\.?\b|\ba\s+year\b",
            re.I,
        ),
    ),
]

_NOT_SPECIFIED_RE = re.compile(
    r"^\s*(?:n/?a|none|not\s+(?:specified|disclosed|provided|stated)|unspecified|tbd|negotiable|competitive|-)\s*$",
    re.I,
)

# Over this, with no period given, an amount in a strong currency is read as a yearly figure.
ANNUAL_THRESHOLD = Decimal(10_000)


@dataclass(frozen=True)
class Pay:
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    currency: str = ""
    period: str = UNKNOWN

    @property
    def parsed(self) -> bool:
        return self.minimum is not None or self.maximum is not None


def _to_decimal(number: str, k_suffix: str | None) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", ""))
    except ArithmeticError:
        return None
    return value * 1000 if k_suffix else value


def _find_period(text: str, near: str) -> str:
    """Prefer a period stated right after the amount, then anywhere in the text."""
    for scope in (near, text):
        for name, pattern in _PERIODS:
            if pattern.search(scope):
                return name
    return UNKNOWN


def parse_pay(text: str | None) -> Pay:
    """Parse text such as `$50 - $80/hr`, `Up to $23/hr`, `KES 80,000 per month` or `Ksh 50k`."""
    if not text or _NOT_SPECIFIED_RE.match(text):
        return Pay()
    text = text.strip()

    # The first amount must carry a currency marker, so "AHT: 6 hours" is never read as money.
    match = None
    for candidate in _AMOUNT_RE.finditer(text):
        if candidate.group("cur"):
            match = candidate
            break
    if match is None:
        return Pay()

    currency = _CURRENCIES[match.group("cur").lower()]
    first = _to_decimal(match.group("num"), match.group("k"))
    if first is None:
        return Pay()
    second: Decimal | None = None
    end = match.end()

    separator = _RANGE_SEP_RE.match(text, end)
    if separator:
        tail = _AMOUNT_RE.match(text, separator.end())
        if tail:
            tail_currency = tail.group("cur")
            if tail_currency is None or _CURRENCIES[tail_currency.lower()] == currency:
                second = _to_decimal(tail.group("num"), tail.group("k"))
                end = tail.end()

    prefix = text[: match.start()]
    near = text[end : end + 40]
    period = _find_period(text, near)
    if second is not None:
        low, high = sorted((first, second))
        minimum: Decimal | None = low
        maximum: Decimal | None = high
    elif _UP_TO_RE.search(prefix):
        minimum, maximum = None, first
    elif _FROM_RE.search(prefix) or re.match(r"\s*\+", text[end:]):
        minimum, maximum = first, None
    else:
        minimum, maximum = first, first

    top = maximum if maximum is not None else minimum
    if (
        period == UNKNOWN
        and top is not None
        and top > ANNUAL_THRESHOLD
        and currency not in _WEAK_CURRENCIES
    ):
        period = "year"
    return Pay(minimum=minimum, maximum=maximum, currency=currency, period=period)
