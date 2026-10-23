"""Profile business logic: versioning, item CRUD and ordering, tracks, completeness, CV import.

Views, tasks and the wizard only call into here. Every function takes the acting user and
refuses to touch another user's rows (`NotOwner`). Any change to the profile bumps
`Profile.version` exactly once, which later phases use to invalidate scores and kits.

Nothing here logs CV text, profile content or file names; logs carry record IDs and counts.
"""

import datetime
import functools
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.validators import URLValidator, validate_email
from django.db import models, transaction
from django.db.models import F, Max
from django.utils import timezone

from apps.accounts.models import User
from apps.core.logging import log_event
from apps.core.tasks import enqueue
from apps.llm.client import call_structured
from apps.llm.exceptions import LLMError
from apps.llm.models import LLMCall
from apps.llm.schemas import ParsedCV
from apps.llm.untrusted import wrap_untrusted

from . import cv_files
from .exceptions import ImportStateError, InvalidCVFile, NotOwner, UnreadableCV
from .models import (
    Certification,
    CVImport,
    Education,
    EmploymentType,
    Experience,
    ExperienceBullet,
    Profile,
    Project,
    Skill,
    Track,
)

logger = logging.getLogger(__name__)

MAX_CV_CHARS = 30_000
ORDERED_MODELS = (Experience, ExperienceBullet, Project, Education, Certification)


# --- profile and versioning ----------------------------------------------


def get_profile(user: User) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


REMATCH_DELAY_SECONDS = 30


def bump_version(profile: Profile) -> None:
    """Atomic increment, so concurrent edits never lose a bump. Also schedules a re-match."""
    Profile.objects.filter(pk=profile.pk).update(version=F("version") + 1)
    profile.refresh_from_db(fields=["version"])
    transaction.on_commit(functools.partial(_schedule_rematch, profile.user_id))


def _schedule_rematch(user_id: int) -> None:
    """Re-run matching for the user shortly after a change. Many edits in a row (an import, a
    burst of bullet edits) share one run: the first schedules it, the rest find it pending, and it
    reads the latest data when it fires."""
    from django.core.cache import cache

    from apps.matching.tasks import rematch_user_task

    if cache.add(f"rematch_pending:{user_id}", True, REMATCH_DELAY_SECONDS):
        rematch_user_task.schedule(args=(user_id,), delay=REMATCH_DELAY_SECONDS)


def save_profile(user: User, profile: Profile) -> None:
    """Persist edited basics (the form has already validated them)."""
    if profile.user_id != user.pk:
        raise NotOwner
    with transaction.atomic():
        profile.save()
        bump_version(profile)


# --- item CRUD (experience, bullets, projects, education, certifications, skills) ---------------


def _check_owner(user: User, obj: models.Model) -> None:
    if getattr(obj, "user_id", None) != user.pk:
        raise NotOwner
    if isinstance(obj, ExperienceBullet) and obj.experience.user_id != user.pk:
        raise NotOwner


def _siblings(obj: models.Model) -> models.QuerySet[Any]:
    """The rows `obj` is ordered among: a bullet's experience, otherwise all the user's rows."""
    manager = type(obj)._default_manager
    if isinstance(obj, ExperienceBullet):
        return manager.filter(experience_id=obj.experience_id)
    return manager.filter(user_id=obj.user_id)  # type: ignore[attr-defined]


def _next_order(obj: models.Model) -> int:
    top = _siblings(obj).aggregate(top=Max("order"))["top"]
    return 0 if top is None else int(top) + 1


def save_item(user: User, obj: Any, *, m2m: dict[str, Iterable[Skill]] | None = None) -> None:
    """Create or update a profile item. `m2m` maps a field name (`skills`, `technologies`) to the
    skills to attach; anything that is not the user's own skill is ignored."""
    is_new = obj.pk is None
    if is_new:
        obj.user = user
    _check_owner(user, obj)
    with transaction.atomic():
        if is_new and isinstance(obj, ORDERED_MODELS):
            obj.order = _next_order(obj)
        obj.save()
        for name, skills in (m2m or {}).items():
            own = [s for s in skills if s.user_id == user.pk]
            getattr(obj, name).set(own)
        bump_version(get_profile(user))


def delete_item(user: User, obj: Any) -> None:
    _check_owner(user, obj)
    with transaction.atomic():
        obj.delete()
        bump_version(get_profile(user))


def move_item(user: User, obj: Any, direction: str) -> None:
    """Swap `obj` with its neighbour (`"up"` or `"down"`) and persist a clean 0..n ordering."""
    _check_owner(user, obj)
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    with transaction.atomic():
        rows = list(_siblings(obj).order_by("order", "id"))
        index = next(i for i, row in enumerate(rows) if row.pk == obj.pk)
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(rows):
            rows[index], rows[target] = rows[target], rows[index]
        for position, row in enumerate(rows):
            row.order = position
        type(obj)._default_manager.bulk_update(rows, ["order"])
        bump_version(get_profile(user))


def find_or_create_skill(
    user: User,
    name: str,
    *,
    category: str = Skill.Category.TECHNICAL,
    level: str = "",
    years: Decimal | None = None,
) -> tuple[Skill, bool]:
    """Case-insensitive get-or-create of one of the user's skills."""
    name = name.strip()[:100]
    existing = Skill.objects.for_user(user).filter(name__iexact=name).first()
    if existing is not None:
        return existing, False
    skill = Skill.objects.create(user=user, name=name, category=category, level=level, years=years)
    return skill, True


# --- tracks ----------------------------------------------


def save_track(user: User, track: Track, *, make_default: bool = False) -> Track:
    """Create or update a track, keeping exactly one default: the first track becomes the default,
    and choosing a new default demotes the old one."""
    is_new = track.pk is None
    if is_new:
        track.user = user
    elif track.user_id != user.pk:
        raise NotOwner
    with transaction.atomic():
        others = Track.objects.for_user(user).exclude(pk=track.pk)
        if not others.exists():
            make_default = True  # the only track is always the default
        if make_default:
            others.filter(is_default=True).update(is_default=False)
            track.is_default = True
        elif is_new:
            track.is_default = False
        track.save()
        bump_version(get_profile(user))
    return track


def delete_track(user: User, track: Track) -> None:
    """Delete a track; if it was the default, the oldest remaining track takes over."""
    if track.user_id != user.pk:
        raise NotOwner
    with transaction.atomic():
        was_default = track.is_default
        track.delete()
        if was_default:
            successor = Track.objects.for_user(user).order_by("created_at", "id").first()
            if successor is not None:
                Track.objects.filter(pk=successor.pk).update(is_default=True)
        bump_version(get_profile(user))


# --- completeness ----------------------------------------------


@dataclass(frozen=True)
class Completeness:
    percent: int
    tips: list[str] = field(default_factory=list)


def completeness(user: User) -> Completeness:
    """A 0-100 score for how usable the profile is, with tips for what to add next."""
    profile = get_profile(user)
    bullets = ExperienceBullet.objects.for_user(user)
    checks: list[tuple[int, bool, str]] = [
        (10, bool(profile.headline.strip()), "Add a headline, such as your target job title."),
        (10, bool(profile.summary.strip()), "Write a short professional summary."),
        (5, bool(profile.phone or profile.public_email), "Add a contact email or phone number."),
        (20, Experience.objects.for_user(user).exists(), "Add at least one work experience."),
        (10, bullets.exists(), "Describe what you did with bullet points under each role."),
        (
            15,
            bullets.exclude(metrics="").exists(),
            "Add metrics to your bullets. Put the real numbers here: the CV generator may "
            "only use numbers you enter.",
        ),
        (
            15,
            Skill.objects.for_user(user).count() >= 8,
            "Add at least 8 skills.",
        ),
        (5, Education.objects.for_user(user).exists(), "Add your education."),
        (
            5,
            Project.objects.for_user(user).exclude(description="").exists(),
            "Describe a project you are proud of.",
        ),
        (5, Track.objects.for_user(user).exists(), "Create a job target (a track)."),
    ]
    earned = sum(weight for weight, ok, _ in checks if ok)
    return Completeness(percent=earned, tips=[tip for _, ok, tip in checks if not ok])


# --- CV import ----------------------------------------------


def create_cv_import(user: User, filename: str, content: bytes) -> CVImport:
    """Validate and store an uploaded CV, then queue parsing.

    Text is extracted once here so a scanned PDF is refused immediately with a friendly message,
    rather than after a wait. The extracted text is not stored.
    """
    kind = cv_files.validate_upload(filename, content)
    cv_files.extract_text(content, kind)  # raises UnreadableCV for scans / empty files
    with transaction.atomic():
        cv_import = CVImport(user=user, original_name=_safe_name(filename))
        cv_import.file.save(filename, ContentFile(content), save=False)
        cv_import.save()
        transaction.on_commit(lambda: _enqueue_parse(cv_import.pk))
    log_event(
        logger, logging.INFO, "cv_import_created", "CV uploaded",
        cv_import_id=cv_import.pk, kind=kind, size=len(content),
    )  # fmt: skip
    return cv_import


def _safe_name(filename: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[:200]


def _enqueue_parse(cv_import_id: int) -> None:
    from .tasks import parse_cv_import

    enqueue(parse_cv_import, cv_import_id)


def run_cv_parse(cv_import_id: int) -> None:
    """Parse an uploaded CV with the LLM. Idempotent: only an `uploaded` import is processed."""
    claimed = CVImport.objects.filter(pk=cv_import_id, status=CVImport.Status.UPLOADED).update(
        status=CVImport.Status.PARSING
    )
    if not claimed:
        return
    cv_import = CVImport.objects.select_related("user").get(pk=cv_import_id)
    try:
        with cv_import.file.open("rb") as handle:
            content = handle.read()
        kind = cv_files.validate_upload(str(cv_import.file.name), content)
        text = cv_files.extract_text(content, kind)
        parsed = call_structured(
            task=LLMCall.Task.PARSE_CV,
            prompt_name="parse_cv",
            variables={"cv": wrap_untrusted("cv", text, MAX_CV_CHARS)},
            schema=ParsedCV,
            user=cv_import.user,
            max_output_tokens=4000,
        )
    except (InvalidCVFile, UnreadableCV) as exc:
        _fail_import(cv_import, exc.message, reason=type(exc).__name__)
        return
    except LLMError as exc:
        _fail_import(cv_import, exc.message, reason=type(exc).__name__)
        return
    except OSError:
        _fail_import(cv_import, "We could not open the uploaded file.", reason="OSError")
        return

    cleaned, dropped = sanitise_parsed(parsed, text)
    cv_import.parsed_data = cleaned.model_dump()
    cv_import.status = CVImport.Status.PARSED
    cv_import.error = ""
    cv_import.save(update_fields=["parsed_data", "status", "error", "updated_at"])
    log_event(
        logger, logging.INFO, "cv_import_parsed", "CV parsed",
        cv_import_id=cv_import.pk, dropped_unsupported=dropped,
    )  # fmt: skip


def _fail_import(cv_import: CVImport, message: str, *, reason: str) -> None:
    cv_import.status = CVImport.Status.FAILED
    cv_import.error = message[:300]
    cv_import.save(update_fields=["status", "error", "updated_at"])
    log_event(
        logger, logging.WARNING, "cv_import_failed", "CV import failed",
        cv_import_id=cv_import.pk, reason=reason,
    )  # fmt: skip


_NUMBER_RE = re.compile(r"\d[\d,.]*")


def _numbers(text: str) -> set[str]:
    return {n.strip(",.").replace(",", "") for n in _NUMBER_RE.findall(text) if n.strip(",.")}


def sanitise_parsed(parsed: ParsedCV, source_text: str) -> tuple[ParsedCV, int]:
    """Enforce "extract only what the CV states" (spec §15.4) on the model's output.

    A bullet's metrics are dropped unless every number in them appears in the CV text, and a
    skill is dropped unless its name appears in the CV text. Returns the cleaned data and how many
    values were removed.
    """
    haystack = source_text.lower()
    cv_numbers = _numbers(source_text)
    dropped = 0
    data = parsed.model_copy(deep=True)

    def skill_supported(name: str) -> bool:
        return bool(name.strip()) and name.strip().lower() in haystack

    for experience in data.experiences:
        for bullet in experience.bullets:
            if bullet.metrics and not _numbers(bullet.metrics) <= cv_numbers:
                bullet.metrics = None
                dropped += 1
            kept = [s for s in bullet.skills if skill_supported(s)]
            dropped += len(bullet.skills) - len(kept)
            bullet.skills = kept
    for project in data.projects:
        kept = [t for t in project.technologies if skill_supported(t)]
        dropped += len(project.technologies) - len(kept)
        project.technologies = kept
    supported_skills = [s for s in data.skills if skill_supported(s.name)]
    dropped += len(data.skills) - len(supported_skills)
    data.skills = supported_skills
    return data, dropped


# --- applying an import ----------------------------------------------


def parse_partial_date(value: str | None) -> datetime.date | None:
    """`2021-03-15`, `2021-03`, `2021` or `Mar 2021` -> a date (day/month default to 1)."""
    if not value:
        return None
    text = value.strip()
    if text.lower() in {"present", "current", "now", "to date", "ongoing"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%b %Y", "%B %Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()  # noqa: DTZ007 - a date, no zone
        except ValueError:
            continue
    if re.fullmatch(r"\d{4}", text):
        return datetime.date(int(text), 1, 1)
    return None


def _clip(model: type[models.Model], name: str, value: str | None) -> str:
    max_length: int = getattr(model._meta.get_field(name), "max_length", None) or 0
    return (value or "").strip()[:max_length]


def _safe_url(value: str | None) -> str:
    candidate = (value or "").strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return ""
    try:
        URLValidator(schemes=["http", "https"])(candidate)
    except ValidationError:
        return ""
    return candidate[:200]


def _safe_email(value: str | None) -> str:
    candidate = (value or "").strip()
    try:
        validate_email(candidate)
    except ValidationError:
        return ""
    return candidate


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value)).quantize(Decimal("0.1"))
    except InvalidOperation:
        return None
    return number if 0 <= number < 100 else None


def _choice(value: str | None, valid: Iterable[str], default: str = "") -> str:
    candidate = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return candidate if candidate in set(valid) else default


@dataclass
class Selection:
    """Which parsed items to apply, as indexes into each list. `None` means all of them."""

    experiences: set[int] | None = None
    projects: set[int] | None = None
    education: set[int] | None = None
    certifications: set[int] | None = None
    skills: set[int] | None = None

    @staticmethod
    def pick[T](items: list[T], chosen: set[int] | None) -> list[T]:
        return [item for i, item in enumerate(items) if chosen is None or i in chosen]


@dataclass(frozen=True)
class ApplyResult:
    experiences: int
    bullets: int
    projects: int
    education: int
    certifications: int
    skills: int


def _exp_key(title: str, company: str, start: datetime.date | None) -> tuple[str, str, Any]:
    return (title.strip().lower(), company.strip().lower(), start)


def apply_import(
    user: User,
    cv_import: CVImport,
    *,
    mode: str,
    basics: dict[str, Any] | None = None,
    selection: Selection | None = None,
) -> ApplyResult:
    """Write a parsed CV into the profile in one transaction.

    `mode="replace"` clears the user's experience, projects, education, certifications and skills
    first. `mode="merge"` only adds items that are not already there and only fills empty basics.
    `basics` overrides the parsed basic fields with what the user confirmed on the review screen.
    """
    if cv_import.user_id != user.pk:
        raise NotOwner
    if mode not in ("replace", "merge"):
        raise ValueError("mode must be 'replace' or 'merge'")
    if cv_import.status != CVImport.Status.PARSED or not cv_import.parsed_data:
        raise ImportStateError
    parsed = ParsedCV.model_validate(cv_import.parsed_data)
    selection = selection or Selection()

    with transaction.atomic():
        profile = get_profile(user)
        if mode == "replace":
            for model in (Experience, Project, Education, Certification, Skill):
                model.objects.for_user(user).delete()
        _apply_basics(profile, parsed, basics or {}, replace=mode == "replace")
        skill_count = _apply_skills(user, parsed, selection)
        exp_count, bullet_count = _apply_experiences(user, parsed, selection)
        proj_count = _apply_projects(user, parsed, selection)
        edu_count = _apply_education(user, parsed, selection)
        cert_count = _apply_certifications(user, parsed, selection)
        profile.save()
        bump_version(profile)
        cv_import.status = CVImport.Status.APPLIED
        cv_import.applied_at = timezone.now()
        cv_import.save(update_fields=["status", "applied_at", "updated_at"])

    result = ApplyResult(exp_count, bullet_count, proj_count, edu_count, cert_count, skill_count)
    log_event(
        logger, logging.INFO, "cv_import_applied", "CV import applied",
        cv_import_id=cv_import.pk, mode=mode, **result.__dict__,
    )  # fmt: skip
    return result


def _apply_basics(
    profile: Profile, parsed: ParsedCV, override: dict[str, Any], *, replace: bool
) -> None:
    candidates: dict[str, Any] = {
        "headline": _clip(Profile, "headline", parsed.headline),
        "summary": _clip(Profile, "summary", parsed.summary),
        "phone": _clip(Profile, "phone", parsed.phone),
        "public_email": _safe_email(parsed.email),
        "location_text": _clip(Profile, "location_text", parsed.location),
        "years_experience": _decimal(parsed.years_experience),
    }
    for key, value in override.items():
        if key in candidates:
            candidates[key] = value
    for key, value in candidates.items():
        current = getattr(profile, key)
        if replace or current in ("", None):
            setattr(profile, key, value if value is not None or replace else current)
    links = [
        {"label": link.label.strip()[:60], "url": _safe_url(link.url)}
        for link in parsed.links
        if _safe_url(link.url)
    ]
    known = {link.get("url") for link in profile.links}
    if replace:
        profile.links = links
    else:
        profile.links = [*profile.links, *[ln for ln in links if ln["url"] not in known]]


def _apply_skills(user: User, parsed: ParsedCV, selection: Selection) -> int:
    created = 0
    for item in Selection.pick(parsed.skills, selection.skills):
        if not item.name.strip():
            continue
        _, was_created = find_or_create_skill(
            user,
            item.name,
            category=_choice(item.category, Skill.Category.values, Skill.Category.TECHNICAL),
            level=_choice(item.level, Skill.Level.values),
            years=_decimal(item.years),
        )
        created += int(was_created)
    return created


def _skills_for(user: User, names: list[str]) -> list[Skill]:
    return [find_or_create_skill(user, name)[0] for name in names if name.strip()]


def _apply_experiences(user: User, parsed: ParsedCV, selection: Selection) -> tuple[int, int]:
    existing = {
        _exp_key(e.title, e.company, e.start_date) for e in Experience.objects.for_user(user)
    }
    order = _order_start(Experience, user)
    exp_count = bullet_count = 0
    for item in Selection.pick(parsed.experiences, selection.experiences):
        start = parse_partial_date(item.start_date)
        key = _exp_key(item.title, item.company, start)
        if not item.title.strip() or not item.company.strip() or key in existing:
            continue
        existing.add(key)
        experience = Experience.objects.create(
            user=user,
            title=_clip(Experience, "title", item.title),
            company=_clip(Experience, "company", item.company),
            location=_clip(Experience, "location", item.location),
            employment_type=_choice(item.employment_type, EmploymentType.values),
            start_date=start,
            end_date=None if item.is_current else parse_partial_date(item.end_date),
            description=_clip(Experience, "description", item.description),
            order=order,
        )
        order += 1
        exp_count += 1
        for position, bullet in enumerate(item.bullets):
            if not bullet.text.strip():
                continue
            row = ExperienceBullet.objects.create(
                user=user,
                experience=experience,
                text=_clip(ExperienceBullet, "text", bullet.text),
                metrics=_clip(ExperienceBullet, "metrics", bullet.metrics),
                order=position,
            )
            row.skills.set(_skills_for(user, bullet.skills))
            bullet_count += 1
    return exp_count, bullet_count


def _apply_projects(user: User, parsed: ParsedCV, selection: Selection) -> int:
    existing = {p.name.strip().lower() for p in Project.objects.for_user(user)}
    order = _order_start(Project, user)
    count = 0
    for item in Selection.pick(parsed.projects, selection.projects):
        if not item.name.strip() or item.name.strip().lower() in existing:
            continue
        existing.add(item.name.strip().lower())
        project = Project.objects.create(
            user=user,
            name=_clip(Project, "name", item.name),
            role=_clip(Project, "role", item.role),
            url=_safe_url(item.url),
            description=_clip(Project, "description", item.description),
            start_date=parse_partial_date(item.start_date),
            end_date=parse_partial_date(item.end_date),
            highlights=[h.strip()[:300] for h in item.highlights if h.strip()][:10],
            order=order,
        )
        order += 1
        project.technologies.set(_skills_for(user, item.technologies))
        count += 1
    return count


def _apply_education(user: User, parsed: ParsedCV, selection: Selection) -> int:
    existing = {
        (e.institution.strip().lower(), e.qualification.strip().lower())
        for e in Education.objects.for_user(user)
    }
    order = _order_start(Education, user)
    count = 0
    for item in Selection.pick(parsed.education, selection.education):
        key = (item.institution.strip().lower(), (item.qualification or "").strip().lower())
        if not item.institution.strip() or key in existing:
            continue
        existing.add(key)
        Education.objects.create(
            user=user,
            institution=_clip(Education, "institution", item.institution),
            qualification=_clip(Education, "qualification", item.qualification),
            field=_clip(Education, "field", item.field),
            start_date=parse_partial_date(item.start_date),
            end_date=parse_partial_date(item.end_date),
            grade=_clip(Education, "grade", item.grade),
            notes=_clip(Education, "notes", item.notes),
            order=order,
        )
        order += 1
        count += 1
    return count


def _apply_certifications(user: User, parsed: ParsedCV, selection: Selection) -> int:
    existing = {c.name.strip().lower() for c in Certification.objects.for_user(user)}
    order = _order_start(Certification, user)
    count = 0
    for item in Selection.pick(parsed.certifications, selection.certifications):
        if not item.name.strip() or item.name.strip().lower() in existing:
            continue
        existing.add(item.name.strip().lower())
        Certification.objects.create(
            user=user,
            name=_clip(Certification, "name", item.name),
            issuer=_clip(Certification, "issuer", item.issuer),
            issued_on=parse_partial_date(item.issued_on),
            expires_on=parse_partial_date(item.expires_on),
            credential_url=_safe_url(item.credential_url),
            status=_choice(item.status, Certification.Status.values, Certification.Status.EARNED),
            order=order,
        )
        order += 1
        count += 1
    return count


def _order_start(model: type[models.Model], user: User) -> int:
    top = model._default_manager.filter(user=user).aggregate(top=Max("order"))["top"]
    return 0 if top is None else int(top) + 1
