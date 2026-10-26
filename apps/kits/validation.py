"""The fabrication and quality validator for generated CVs (spec §12.5).

The model may only choose, order and rephrase what is in the master profile. Every claim cites
profile item IDs, and this module checks the citations: the IDs must exist and belong to the user,
every number must appear in a cited item, skills the job asks for may only be named when the user
has them, and the house style is respected. Pure functions: no database, no model, no logging.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.llm.schemas import CVBullet, CVData, CVExperience, CVProject

from . import style

MAX_BULLET_WORDS = 30
MAX_SUMMARY_WORDS = 60
MAX_SKILLS = 14
MAX_EXPERIENCE_BULLETS = 5
MAX_PROJECTS = 3
MAX_PROJECT_BULLETS = 3
MAX_HEADLINE_CHARS = 160

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_ID_RE = re.compile(r"^([A-Z]{3})-(\d+)$")

# Issue kinds. Style problems that have a mechanical fix are repaired instead of dropped.
ID, SOURCE, NUMBER, SKILL, STYLE, LENGTH = "id", "source", "number", "skill", "style", "length"


@dataclass(frozen=True)
class Issue:
    field: str  # a path such as "experiences[0].bullets[1]", "summary" or "headline"
    message: str
    kind: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message}


# --- text helpers ---------------------------------------------------------------------------


def numbers_in(text: str) -> set[str]:
    """Numbers and percentages in `text`, normalised ("1,200" -> "1200", "40.0%" -> "40")."""
    found: set[str] = set()
    for raw in _NUMBER_RE.findall(text):
        cleaned = raw.replace(",", "").rstrip(".")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        found.add(str(int(value)) if value == int(value) else str(value))
    return found


def _stem(token: str) -> str:
    token = token.rstrip(".")
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def stems(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower())]


def _contains_phrase(tokens: Sequence[str], phrase: Sequence[str]) -> bool:
    size = len(phrase)
    if size == 0:
        return False
    return any(list(tokens[i : i + size]) == list(phrase) for i in range(len(tokens) - size + 1))


def _flatten(value: Any) -> Iterable[str]:
    """Every string and number inside nested profile data, as text."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int | float):
        yield f"{value:g}"
    elif isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _flatten(item)


# --- the profile as the validator sees it ----------------------------------------------------


class ProfileIndex:
    """Looks up profile items by ID and answers "does this item mention that?"."""

    def __init__(self, profile: dict[str, Any]) -> None:
        self.kinds: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.skill_names: list[str] = []
        self._summary_numbers: set[str] = set()
        basics = profile.get("basics", {})
        years = basics.get("years_experience")
        if years is not None:
            self._summary_numbers |= numbers_in(f"{years:g}")

        for exp in profile.get("experiences", []):
            bullets = exp.get("bullets", [])
            self._add(exp["id"], {**exp, "bullets": bullets})
            for bullet in bullets:
                self._add(bullet["id"], bullet)
        for project in profile.get("projects", []):
            self._add(project["id"], project)
        for education in profile.get("education", []):
            self._add(education["id"], education)
        for cert in profile.get("certifications", []):
            self._add(cert["id"], cert)
        for skill in profile.get("skills", []):
            self._add(skill["id"], skill)
            self.skill_names.append(skill["name"])
            if skill.get("years") is not None:
                self._summary_numbers |= numbers_in(f"{skill['years']:g}")
        self._skill_stems = [stems(name) for name in self.skill_names]

    def _add(self, item_id: str, data: dict[str, Any]) -> None:
        self.kinds[item_id] = item_id.split("-")[0]
        self.texts[item_id] = " ".join(_flatten(data))

    def exists(self, item_id: str, kind: str | None = None) -> bool:
        if item_id not in self.kinds:
            return False
        return kind is None or self.kinds[item_id] == kind

    def source_text(self, source_ids: Iterable[str]) -> str:
        return " ".join(self.texts[i] for i in source_ids if i in self.texts)

    def has_skill(self, phrase: Sequence[str]) -> bool:
        return any(_contains_phrase(skill, phrase) for skill in self._skill_stems)

    def allowed_summary_numbers(self) -> set[str]:
        return self._summary_numbers


# --- the checks ------------------------------------------------------------------------------


def _check_claim(
    path: str,
    text: str,
    source_ids: list[str],
    index: ProfileIndex,
    job_skills: Sequence[str],
    *,
    extra_numbers: set[str] | None = None,
) -> list[Issue]:
    """Checks 1-4 for one bullet or the summary: IDs, sources, numbers, skills."""
    issues: list[Issue] = []
    if not source_ids:
        return [Issue(path, "cites no profile item", SOURCE)]
    unknown = [i for i in source_ids if not index.exists(i)]
    if unknown:
        return [Issue(path, f"cites {', '.join(unknown)}, which is not in your profile", ID)]

    source = index.source_text(source_ids)
    allowed = numbers_in(source) | (extra_numbers or set())
    invented = sorted(numbers_in(text) - allowed, key=float)
    if invented:
        issues.append(
            Issue(
                path,
                f"uses {', '.join(invented)}, which is not in the profile items it cites",
                NUMBER,
            )
        )

    claim_stems = stems(text)
    source_stems = stems(source)
    for skill in job_skills:
        phrase = stems(skill)
        if len(skill.strip()) < 3 or not _contains_phrase(claim_stems, phrase):
            continue
        if index.has_skill(phrase) or _contains_phrase(source_stems, phrase):
            continue
        issues.append(Issue(path, f"names “{skill}”, which is not in your profile", SKILL))
    return issues


def _check_text(path: str, text: str, *, max_words: int | None = None) -> list[Issue]:
    issues = [Issue(path, f"{problem}", STYLE) for problem in style.style_issues(text)]
    if max_words is not None and style.word_count(text) > max_words:
        issues.append(Issue(path, f"is longer than {max_words} words", LENGTH))
    if not text.strip():
        issues.append(Issue(path, "is empty", LENGTH))
    return issues


def validate_cv(cv: CVData, profile: dict[str, Any], *, job_skills: Sequence[str]) -> list[Issue]:
    """Every problem found in `cv`, in document order. An empty list means it passed."""
    index = ProfileIndex(profile)
    issues: list[Issue] = []

    issues += _check_text("headline", cv.headline)
    if len(cv.headline) > MAX_HEADLINE_CHARS:
        issues.append(Issue("headline", f"is longer than {MAX_HEADLINE_CHARS} characters", LENGTH))

    issues += _check_text("summary", cv.summary, max_words=MAX_SUMMARY_WORDS)
    issues += _check_claim(
        "summary",
        cv.summary,
        cv.summary_source_ids,
        index,
        job_skills,
        extra_numbers=index.allowed_summary_numbers(),
    )

    if len(cv.skills) > MAX_SKILLS:
        issues.append(Issue("skills", f"lists more than {MAX_SKILLS} skills", LENGTH))
    for i, skill_id in enumerate(cv.skills):
        if not index.exists(skill_id, "SKL"):
            issues.append(Issue(f"skills[{i}]", f"{skill_id} is not one of your skills", ID))

    seen_experiences: set[str] = set()
    for i, exp in enumerate(cv.experiences):
        path = f"experiences[{i}]"
        if not index.exists(exp.experience_id, "EXP"):
            issues.append(Issue(path, f"{exp.experience_id} is not one of your jobs", ID))
            continue
        if exp.experience_id in seen_experiences:
            issues.append(Issue(path, f"{exp.experience_id} appears twice", ID))
        seen_experiences.add(exp.experience_id)
        if len(exp.bullets) > MAX_EXPERIENCE_BULLETS:
            issues.append(Issue(path, f"has more than {MAX_EXPERIENCE_BULLETS} bullets", LENGTH))
        issues += _check_bullets(path, exp.bullets, index, job_skills)

    if len(cv.projects) > MAX_PROJECTS:
        issues.append(Issue("projects", f"lists more than {MAX_PROJECTS} projects", LENGTH))
    for i, project in enumerate(cv.projects):
        path = f"projects[{i}]"
        if not index.exists(project.project_id, "PRJ"):
            issues.append(Issue(path, f"{project.project_id} is not one of your projects", ID))
            continue
        if len(project.bullets) > MAX_PROJECT_BULLETS:
            issues.append(Issue(path, f"has more than {MAX_PROJECT_BULLETS} bullets", LENGTH))
        issues += _check_bullets(path, project.bullets, index, job_skills)

    for i, edu_id in enumerate(cv.education_ids):
        if not index.exists(edu_id, "EDU"):
            issues.append(Issue(f"education_ids[{i}]", f"{edu_id} is not in your education", ID))
    for i, cert_id in enumerate(cv.certification_ids):
        if not index.exists(cert_id, "CRT"):
            issues.append(
                Issue(f"certification_ids[{i}]", f"{cert_id} is not one of your certifications", ID)
            )
    return issues


def _check_bullets(
    path: str, bullets: list[CVBullet], index: ProfileIndex, job_skills: Sequence[str]
) -> list[Issue]:
    issues: list[Issue] = []
    for j, bullet in enumerate(bullets):
        bullet_path = f"{path}.bullets[{j}]"
        issues += _check_text(bullet_path, bullet.text, max_words=MAX_BULLET_WORDS)
        issues += _check_claim(bullet_path, bullet.text, bullet.source_ids, index, job_skills)
    return issues


# --- repair ----------------------------------------------------------------------------------


@dataclass
class Repaired:
    cv: CVData
    dropped: list[Issue] = field(default_factory=list)
    repaired: list[Issue] = field(default_factory=list)


def _only_em_dash(issues: list[Issue]) -> bool:
    return bool(issues) and all(
        i.kind == STYLE and i.message == "contains an em dash" for i in issues
    )


def drop_failing(
    cv: CVData,
    issues: list[Issue],
    *,
    fallback_headline: str,
    fallback_summary: str,
) -> Repaired:
    """The last resort after the retry: repair what can be repaired mechanically (em dashes) and
    remove what cannot (a bullet that cites nothing real, invents a number, names a skill the
    user lacks, or uses a banned phrase). Nothing unverifiable survives.

    A bad headline or summary falls back to the user's own wording from their profile.
    """
    by_field: dict[str, list[Issue]] = {}
    for issue in issues:
        by_field.setdefault(issue.field, []).append(issue)
    result = Repaired(cv=cv.model_copy(deep=True))
    out = result.cv

    def bad(path: str) -> bool:
        found = by_field.get(path, [])
        if not found:
            return False
        if _only_em_dash(found):
            result.repaired += found
            return False
        result.dropped += found
        return True

    # Text that only needs its em dashes replaced is cleaned wherever it sits.
    out.headline = style.strip_em_dashes(out.headline)
    out.summary = style.strip_em_dashes(out.summary)

    if bad("headline"):
        out.headline = fallback_headline
    if bad("summary"):
        out.summary = fallback_summary
        out.summary_source_ids = []

    skills_bad = {i for i in range(len(out.skills)) if bad(f"skills[{i}]")}
    if len(out.skills) > MAX_SKILLS:
        result.dropped += by_field.get("skills", [])
    out.skills = [s for i, s in enumerate(out.skills) if i not in skills_bad][:MAX_SKILLS]

    kept_experiences: list[CVExperience] = []
    for i, exp in enumerate(out.experiences):
        path = f"experiences[{i}]"
        if _bad_item(path, by_field, result):
            continue  # unknown or duplicate job: removed
        exp.bullets = _keep_bullets(path, exp.bullets, bad)[:MAX_EXPERIENCE_BULLETS]
        kept_experiences.append(exp)
    out.experiences = kept_experiences

    kept_projects: list[CVProject] = []
    for i, project in enumerate(out.projects):
        path = f"projects[{i}]"
        if _bad_item(path, by_field, result):
            continue
        project.bullets = _keep_bullets(path, project.bullets, bad)[:MAX_PROJECT_BULLETS]
        kept_projects.append(project)
    out.projects = kept_projects[:MAX_PROJECTS]
    if len(cv.projects) > MAX_PROJECTS:
        result.dropped += by_field.get("projects", [])

    out.education_ids = [
        e for i, e in enumerate(out.education_ids) if not bad(f"education_ids[{i}]")
    ]
    out.certification_ids = [
        c for i, c in enumerate(out.certification_ids) if not bad(f"certification_ids[{i}]")
    ]
    return result


def _bad_item(path: str, by_field: dict[str, list[Issue]], result: Repaired) -> bool:
    """A job or project is removed only for an unknown or duplicate ID; "too many bullets" is
    handled by trimming, so it never costs the whole entry."""
    found = by_field.get(path, [])
    if any(i.kind == ID for i in found):
        result.dropped += [i for i in found if i.kind == ID]
        return True
    result.repaired += found
    return False


def _keep_bullets(path: str, bullets: list[CVBullet], bad: Any) -> list[CVBullet]:
    kept: list[CVBullet] = []
    for j, bullet in enumerate(bullets):
        bullet_path = f"{path}.bullets[{j}]"
        if bad(bullet_path):
            continue
        bullet.text = style.strip_em_dashes(bullet.text)
        kept.append(bullet)
    return kept


def build_report(
    final_issues: list[Issue],
    repaired: Repaired | None = None,
    *,
    retried: bool = False,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """The `Kit.validation_report`: `passed` is true only when nothing had to be removed."""
    dropped = repaired.dropped if repaired else []
    fixed = repaired.repaired if repaired else []
    issues = [*final_issues, *dropped]
    return {
        "passed": not issues,
        "retried": retried,
        "issues": [i.as_dict() for i in issues],
        "repaired": [i.as_dict() for i in fixed],
        "notes": notes or [],
    }


def check_edit(texts: Iterable[tuple[str, str]]) -> list[Issue]:
    """Warnings for text the user edited: banned phrases and em dashes only, never blocking."""
    issues: list[Issue] = []
    for path, text in texts:
        issues += [Issue(path, problem, STYLE) for problem in style.style_issues(text)]
    return issues


LETTER_MIN_WORDS, LETTER_MAX_WORDS = 180, 300


def check_letter(text: str, *, allowed_numbers: set[str]) -> list[Issue]:
    """Style, length and invented-number checks for a cover letter (spec §12.4, §12.5)."""
    issues = [Issue("cover_letter", problem, STYLE) for problem in style.style_issues(text)]
    words = style.word_count(text)
    if words > LETTER_MAX_WORDS:
        issues.append(Issue("cover_letter", f"is longer than {LETTER_MAX_WORDS} words", LENGTH))
    elif words < LETTER_MIN_WORDS:
        issues.append(Issue("cover_letter", f"is shorter than {LETTER_MIN_WORDS} words", LENGTH))
    invented = sorted(numbers_in(text) - allowed_numbers, key=float)
    if invented:
        issues.append(
            Issue(
                "cover_letter",
                f"uses {', '.join(invented)}, which is not in your profile or the job posting",
                NUMBER,
            )
        )
    return issues
