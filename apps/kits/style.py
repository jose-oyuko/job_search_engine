"""House writing style for generated documents (spec §12.4).

The prompts ask for this style; the validator enforces the parts that can be checked mechanically:
banned words and phrases, and em dashes.
"""

import re

# Label shown to the user -> pattern. Keep in sync with the writing prompts.
_BANNED: dict[str, str] = {
    "leverage": r"\bleverag(?:e|es|ed|ing)\b",
    "spearheaded": r"\bspearhead(?:s|ed|ing)?\b",
    "synergy": r"\bsynerg(?:y|ies|istic)\b",
    "dynamic": r"\bdynamic\b",
    "passionate": r"\bpassionate(?:ly)?\b",
    "results-driven": r"\bresults[- ]driven\b",
    "proven track record": r"\bproven track record\b",
    "go-getter": r"\bgo[- ]getter\b",
    "cutting-edge": r"\bcutting[- ]edge\b",
    "seamless": r"\bseamless(?:ly)?\b",
    "robust": r"\brobust(?:ly|ness)?\b",
    "detail-oriented professional": r"\bdetail[- ]oriented professional\b",
    "I am excited to apply": r"\bI am excited to apply\b",
    "I am writing to express my interest": r"\bI am writing to express my interest\b",
    "thrilled": r"\bthrilled\b",
    "delve": r"\bdelv(?:e|es|ed|ing)\b",
    "tapestry": r"\btapestry\b",
    "in today's fast-paced": r"\bin today[’']s fast[- ]paced\b",
    "hit the ground running": r"\bhit(?:ting)? the ground running\b",
    "team player": r"\bteam[- ]players?\b",
    "think outside the box": r"\bthink(?:ing)? outside (?:of )?the box\b",
    "utilize": r"\butili[sz](?:e|es|ed|ing)\b",
}
_BANNED_RES = {label: re.compile(pattern, re.IGNORECASE) for label, pattern in _BANNED.items()}
BANNED_PHRASES = tuple(_BANNED)

EM_DASHES = ("—", "―")
_EM_DASH_RE = re.compile(r"\s*[—―]\s*")


def find_banned(text: str) -> list[str]:
    """The banned words and phrases that appear in `text`, once each, in list order."""
    return [label for label, pattern in _BANNED_RES.items() if pattern.search(text)]


def has_em_dash(text: str) -> bool:
    return any(dash in text for dash in EM_DASHES)


def style_issues(text: str) -> list[str]:
    """Plain-words problems with `text`, for the validator report and edit warnings."""
    issues = [f"uses the banned phrase “{label}”" for label in find_banned(text)]
    if has_em_dash(text):
        issues.append("contains an em dash")
    return issues


def strip_em_dashes(text: str) -> str:
    """Mechanical repair: an em dash becomes a comma (the style guide allows commas)."""
    return _EM_DASH_RE.sub(", ", text)


def word_count(text: str) -> int:
    return len(text.split())
