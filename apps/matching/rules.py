"""The deterministic rule filter (spec §10.2). No LLM and no database access.

For one job and one track (and the user's country and skills) it answers: does this job pass, and
why or why not, in plain words. Unknown facts never exclude a job; they pass with a note, so the
user still sees the job and can judge it.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from apps.jobs.eligibility import is_eligible
from apps.jobs.services import normalise_company


class JobLike(Protocol):
    title: str
    company: str
    description_text: str
    work_type: str
    locations_raw: list[str]
    eligible_countries: list[str]
    eligible_regions: list[str]
    is_global: bool
    eligibility_known: bool
    pay_min: Decimal | None
    pay_max: Decimal | None
    pay_currency: str
    pay_period: str
    extracted: dict[str, Any] | None


class TrackLike(Protocol):
    target_titles: list[str]
    include_keywords: list[str]
    exclude_keywords: list[str]
    work_types: list[str]
    locations: list[str]
    min_pay_amount: Decimal | None
    min_pay_currency: str
    min_pay_period: str
    excluded_companies: list[str]


@dataclass(frozen=True)
class RuleResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    relevance: int = 0  # how many keyword and skill hits: used to pick the best track


def _term_pattern(term: str) -> re.Pattern[str]:
    # Whole-word style match that also works for terms such as "C++" or ".NET".
    return re.compile(rf"(?<![\w]){re.escape(term.strip())}(?![\w])", re.IGNORECASE)


def _hits(terms: Iterable[str], text: str) -> list[str]:
    return [t.strip() for t in terms if t.strip() and _term_pattern(t).search(text)]


def _required_skills(job: JobLike) -> list[str]:
    extracted = job.extracted or {}
    skills = extracted.get("required_skills")
    return [str(s) for s in skills] if isinstance(skills, list) else []


# Pay periods we can compare directly or convert exactly.
_MONTHS_PER_YEAR = Decimal(12)


def _comparable_amount(amount: Decimal, from_period: str, to_period: str) -> Decimal | None:
    """`amount` expressed per `to_period`, or None if the two periods cannot be compared."""
    if from_period == to_period:
        return amount
    if from_period == "month" and to_period == "year":
        return amount * _MONTHS_PER_YEAR
    if from_period == "year" and to_period == "month":
        return amount / _MONTHS_PER_YEAR
    return None


def evaluate(
    job: JobLike, track: TrackLike, *, country: str, skill_names: Sequence[str]
) -> RuleResult:
    """Run every rule. All must hold for the job to pass; reasons explain each decision."""
    reasons: list[str] = []
    failed = False

    # 1. Eligible for the user's country. Unknown passes (with a note).
    eligible = is_eligible(job, country)
    if eligible is False:
        reasons.append(f"not open to {country}")
        failed = True
    elif eligible is True:
        reasons.append(f"eligible: {'global' if job.is_global else country}")
    else:
        reasons.append("eligibility unclear")

    # 2. Work type, and location for on-site or hybrid work.
    work_type = job.work_type
    if track.work_types and work_type not in ("", "unknown") and work_type not in track.work_types:
        reasons.append(f"work type {work_type} not wanted")
        failed = True
    elif work_type in ("", "unknown"):
        reasons.append("work type unknown")
    else:
        reasons.append(f"work type: {work_type}")
        if work_type in ("onsite", "hybrid"):
            location_ok, note = _location_ok(job, track)
            reasons.append(note)
            failed = failed or not location_ok

    # 3. Pay, compared only in the same currency and a comparable period.
    pay_ok, pay_note = _pay_ok(job, track)
    reasons.append(pay_note)
    failed = failed or not pay_ok

    # 4. Not excluded: company and title keywords.
    company = normalise_company(job.company)
    if any(company and normalise_company(c) == company for c in track.excluded_companies):
        reasons.append(f"excluded company: {job.company}")
        failed = True
    excluded_words = _hits(track.exclude_keywords, job.title)
    if excluded_words:
        reasons.append(f"excluded keyword in title: {excluded_words[0]}")
        failed = True

    # 5. Relevant: a target title or keyword, or at least two of the user's skills.
    haystack = f"{job.title}\n{job.description_text}"
    keyword_hits = _hits([*track.target_titles, *track.include_keywords], haystack)
    skill_text = f"{haystack}\n" + "\n".join(_required_skills(job))
    skill_hits = _hits(skill_names, skill_text)
    if keyword_hits:
        reasons.append(f"keyword: {keyword_hits[0].lower()}")
    if len(skill_hits) >= 2:
        reasons.append(f"skills: {', '.join(h.lower() for h in skill_hits[:3])}")
    relevant = bool(keyword_hits) or len(skill_hits) >= 2
    if not relevant:
        reasons.append("no matching keywords or skills")
        failed = True

    return RuleResult(
        passed=not failed,
        reasons=reasons,
        relevance=len(set(h.lower() for h in keyword_hits))
        + len(set(h.lower() for h in skill_hits)),
    )


def _location_ok(job: JobLike, track: TrackLike) -> tuple[bool, str]:
    wanted = [loc.strip().lower() for loc in track.locations if loc.strip()]
    if not wanted:
        return True, "on-site: no location preference set"
    places = [p.lower() for p in job.locations_raw]
    if not places:
        return True, "on-site location unknown"
    if any(w in p or p in w for w in wanted for p in places):
        return True, "location matches"
    return False, "on-site location does not match"


def _pay_ok(job: JobLike, track: TrackLike) -> tuple[bool, str]:
    minimum = track.min_pay_amount
    if minimum is None:
        return True, "no minimum pay set"
    offered = job.pay_max if job.pay_max is not None else job.pay_min
    if offered is None:
        return True, "pay unknown"
    if job.pay_currency.upper() != track.min_pay_currency.upper():
        return True, f"pay in {job.pay_currency or 'unknown currency'}, not compared"
    comparable = _comparable_amount(offered, job.pay_period, track.min_pay_period)
    if comparable is None:
        return True, f"pay per {job.pay_period} is not comparable to per {track.min_pay_period}"
    if comparable < minimum:
        return False, "pay below your minimum"
    return True, "pay meets your minimum"
