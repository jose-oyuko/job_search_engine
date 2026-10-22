"""Simple scam heuristics (spec §7.7). Flags are warnings only: jobs are never hidden by them."""

import re
from decimal import Decimal

FLAG_ASKS_PAYMENT = "asks_for_payment"
FLAG_ASKS_ID_EARLY = "asks_for_id_or_bank_details"
FLAG_MESSAGING_ONLY = "messaging_app_contact"
FLAG_PAY_HIGH = "pay_unusually_high"

FLAG_LABELS = {
    FLAG_ASKS_PAYMENT: "Asks you to pay a fee",
    FLAG_ASKS_ID_EARLY: "Asks for ID or bank details early",
    FLAG_MESSAGING_ONLY: "Contact only through a messaging app",
    FLAG_PAY_HIGH: "Pay is unusually high",
}

_PAYMENT_RE = re.compile(
    r"(registration|training|processing|application|admin(?:istration)?|starter\s*kit|equipment)\s+fee"
    r"|(?:pay|send|deposit)\s+(?:a\s+|an\s+|the\s+)?(?:small\s+)?(?:fee|deposit)"
    r"|(?:purchase|buy)\s+(?:your\s+own\s+)?(?:equipment|laptop|software)\s+from\s+us"
    r"|refundable\s+deposit|upfront\s+(?:fee|payment)",
    re.IGNORECASE,
)
_ID_EARLY_RE = re.compile(
    r"(?:send|provide|submit|share)\s+(?:us\s+)?(?:a\s+)?(?:copy\s+of\s+)?"
    r"(?:your\s+)?(?:passport|national\s+id|id\s+card|bank\s+(?:account|details|statement)|ssn|social\s+security)"
    r"[^.]{0,60}\b(?:before|prior\s+to|to\s+(?:apply|get\s+started|begin))",
    re.IGNORECASE,
)
_MESSAGING_RE = re.compile(
    r"(?:contact|message|reach|text|apply)\s+(?:us\s+|me\s+|our\s+\w+\s+)?(?:on|via|through|at|using)\s+"
    r"(?:whatsapp|telegram|signal)"
    r"|(?:whatsapp|telegram|signal)\s*(?:us\s*|me\s*)?(?::|on|at|only|number|handle|@)\s*[+@\w]"
    r"|t\.me/|wa\.me/",
    re.IGNORECASE,
)

# Generous ceilings per period (USD). Legitimate expert work can pay well, so only extremes flag.
_HIGH_PAY_USD: dict[str, Decimal] = {
    "hour": Decimal(400),
    "task": Decimal(3000),
    "day": Decimal(3000),
    "week": Decimal(15000),
    "month": Decimal(40000),
}


def detect_scam_flags(
    text: str, *, pay_max: Decimal | None = None, pay_currency: str = "", pay_period: str = ""
) -> list[str]:
    """Return the flag codes that apply to a job's description text and pay."""
    flags: list[str] = []
    if _PAYMENT_RE.search(text):
        flags.append(FLAG_ASKS_PAYMENT)
    if _ID_EARLY_RE.search(text):
        flags.append(FLAG_ASKS_ID_EARLY)
    if _MESSAGING_RE.search(text):
        flags.append(FLAG_MESSAGING_ONLY)
    ceiling = _HIGH_PAY_USD.get(pay_period)
    if ceiling is not None and pay_max is not None and pay_currency == "USD" and pay_max > ceiling:
        flags.append(FLAG_PAY_HIGH)
    return flags
