"""Stable serialisation of the profile for scoring and kit generation (spec §5.3, §12.5).

Every item carries a stable ID derived from its primary key (`EXP-12`, `BUL-88`, ...). The kit
generator may only cite these IDs, which is how fabricated claims are caught. Output is
deterministic: fixed key order, fixed list order, no timestamps.
"""

import datetime
import re
from typing import Any

from django.core.cache import cache

from apps.accounts.models import User

from .models import (
    Certification,
    Education,
    Experience,
    ExperienceBullet,
    Profile,
    Project,
    Skill,
    Track,
)
from .services import get_profile

PREFIXES = {
    "EXP": Experience,
    "BUL": ExperienceBullet,
    "PRJ": Project,
    "EDU": Education,
    "CRT": Certification,
    "SKL": Skill,
    "TRK": Track,
}
_REF_RE = re.compile(r"^([A-Z]{3})-(\d+)$")
SUMMARY_TIMEOUT = 7 * 24 * 3600
_LEVEL_RANK = {"expert": 4, "advanced": 3, "intermediate": 2, "basic": 1, "": 0}


def ref(prefix: str, pk: int) -> str:
    return f"{prefix}-{pk}"


def parse_ref(value: str) -> tuple[str, int] | None:
    """`"EXP-12"` -> `("EXP", 12)`; anything else -> None."""
    match = _REF_RE.match(value.strip())
    if not match or match.group(1) not in PREFIXES:
        return None
    return match.group(1), int(match.group(2))


def _month(value: datetime.date | None) -> str | None:
    return value.strftime("%Y-%m") if value else None


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def serialize_profile(user: User) -> dict[str, Any]:
    """The whole master profile as plain data, in a fixed order."""
    profile: Profile = get_profile(user)
    skills = list(Skill.objects.for_user(user).order_by("category", "name", "id"))
    skill_names = {s.pk: s.name for s in skills}
    experiences = (
        Experience.objects.for_user(user)
        .prefetch_related("bullets__skills")
        .order_by("order", "id")
    )
    return {
        "version": profile.version,
        "basics": {
            "headline": profile.headline,
            "summary": profile.summary,
            "phone": profile.phone,
            "email": profile.public_email,
            "location": profile.location_text,
            "links": profile.links,
            "years_experience": _num(profile.years_experience),
        },
        "experiences": [
            {
                "id": ref("EXP", e.pk),
                "title": e.title,
                "company": e.company,
                "location": e.location,
                "employment_type": e.employment_type,
                "start": _month(e.start_date),
                "end": _month(e.end_date),
                "current": e.end_date is None,
                "description": e.description,
                "bullets": [
                    {
                        "id": ref("BUL", b.pk),
                        "text": b.text,
                        "metrics": b.metrics,
                        "skills": sorted(skill.name for skill in b.skills.all()),
                    }
                    for b in sorted(e.bullets.all(), key=lambda b: (b.order, b.pk))
                ],
            }
            for e in experiences
        ],
        "projects": [
            {
                "id": ref("PRJ", p.pk),
                "name": p.name,
                "role": p.role,
                "url": p.url,
                "description": p.description,
                "technologies": sorted(skill_names[t.pk] for t in p.technologies.all()),
                "start": _month(p.start_date),
                "end": _month(p.end_date),
                "highlights": p.highlights,
            }
            for p in Project.objects.for_user(user).prefetch_related("technologies")
        ],
        "education": [
            {
                "id": ref("EDU", e.pk),
                "institution": e.institution,
                "qualification": e.qualification,
                "field": e.field,
                "start": _month(e.start_date),
                "end": _month(e.end_date),
                "grade": e.grade,
                "notes": e.notes,
            }
            for e in Education.objects.for_user(user)
        ],
        "certifications": [
            {
                "id": ref("CRT", c.pk),
                "name": c.name,
                "issuer": c.issuer,
                "issued": _month(c.issued_on),
                "expires": _month(c.expires_on),
                "credential_url": c.credential_url,
                "status": c.status,
            }
            for c in Certification.objects.for_user(user)
        ],
        "skills": [
            {
                "id": ref("SKL", s.pk),
                "name": s.name,
                "category": s.category,
                "level": s.level,
                "years": _num(s.years),
            }
            for s in skills
        ],
    }


def profile_summary(user: User) -> str:
    """A compact, deterministic text summary for LLM scoring, cached per profile version."""
    profile = get_profile(user)
    key = f"profile_summary:{user.pk}:{profile.version}"
    cached: str | None = cache.get(key)
    if cached is not None:
        return cached
    summary = _build_summary(serialize_profile(user))
    cache.set(key, summary, SUMMARY_TIMEOUT)
    return summary


def _span(start: str | None, end: str | None, current: bool) -> str:
    if not start and not end:
        return ""
    return f" ({start or '?'} to {'present' if current else (end or '?')})"


def _build_summary(data: dict[str, Any]) -> str:
    basics = data["basics"]
    lines: list[str] = []
    if basics["headline"]:
        lines.append(f"Headline: {basics['headline']}")
    if basics["years_experience"] is not None:
        lines.append(f"Years of experience: {basics['years_experience']:g}")
    if basics["location"]:
        lines.append(f"Location: {basics['location']}")
    skills = sorted(
        data["skills"],
        key=lambda s: (-_LEVEL_RANK.get(s["level"], 0), -(s["years"] or 0), s["name"].lower()),
    )[:25]
    if skills:
        lines.append("Skills: " + ", ".join(s["name"] for s in skills))
    if data["experiences"]:
        lines.append("Experience:")
        for e in data["experiences"][:6]:
            span = _span(e["start"], e["end"], e["current"])
            lines.append(f"- {e['title']} at {e['company']}{span}")
    if data["education"]:
        lines.append(
            "Education: "
            + "; ".join(
                " ".join(p for p in (e["qualification"], e["field"]) if p)
                + (f", {e['institution']}" if e["institution"] else "")
                for e in data["education"][:3]
            )
        )
    certs = [c["name"] for c in data["certifications"] if c["status"] == "earned"][:6]
    if certs:
        lines.append("Certifications: " + ", ".join(certs))
    return "\n".join(lines)
