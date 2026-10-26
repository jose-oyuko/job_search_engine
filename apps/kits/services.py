"""Kit generation: the pipeline, edits, versions and quota checks (spec §12).

Views, Huey tasks and the matching hook are thin wrappers over this module. Nothing here logs
profile text, job text, prompts or documents; only IDs, counts and event names.
"""

import contextlib
import datetime
import functools
import json
import logging
from dataclasses import dataclass
from typing import Any

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User, UserSettings
from apps.core.logging import log_event
from apps.jobs.models import Job
from apps.llm.client import call_structured, call_text
from apps.llm.exceptions import LLMError, QuotaExceeded
from apps.llm.models import LLMCall
from apps.llm.schemas import (
    CVBullet,
    CVData,
    CVExperience,
    InterviewPrep,
    ScreeningSet,
)
from apps.llm.services import check_quota
from apps.llm.untrusted import wrap_untrusted
from apps.matching.models import Match
from apps.profiles.models import Experience, Profile, Project, Skill, Track
from apps.profiles.serializers import profile_summary, serialize_profile

from . import documents, hooks, style, validation
from .exceptions import EmptyProfile, KitInProgress, KitTooOld, NotOwner
from .models import Kit

logger = logging.getLogger(__name__)

STUCK_AFTER = datetime.timedelta(minutes=10)
MAX_INSTRUCTION_CHARS = 300
JOB_PROMPT_CHARS = 8000
PROFILE_PROMPT_CHARS = 24_000
LETTER_MIN_WORDS, LETTER_MAX_WORDS = 180, 300
MAX_INTERVIEW_ITEMS = 15
FILE_KEYS = ("cv_pdf", "cv_docx", "cover_letter_pdf", "cover_letter_docx")

STEP_CV = "Selecting your most relevant experience…"
STEP_LETTER = "Writing cover letter…"
STEP_INTERVIEW = "Preparing interview questions…"
STEP_SCREENING = "Drafting screening answers…"
STEP_RENDER = "Rendering documents…"

_LEVEL_RANK = {"expert": 4, "advanced": 3, "intermediate": 2, "basic": 1, "": 0}


# --- quota and lookups -----------------------------------------------------------------------


def kits_exhausted(user: User) -> bool:
    """True when the user's monthly kit allowance (or spend cap) is used up."""
    try:
        check_quota(user, LLMCall.Task.GENERATE_CV)
    except QuotaExceeded:
        return True
    return False


def get_kit(user: User, kit_id: int) -> Kit:
    kit = Kit.objects.for_user(user).select_related("job", "user").filter(pk=kit_id).first()
    if kit is None:
        raise NotOwner("Kit not found.")
    return kit


def kit_versions(user: User, job: Job) -> list[Kit]:
    """The user's kits for one job, newest first."""
    return list(Kit.objects.for_user(user).filter(job=job).order_by("-version", "-id"))


def latest_ready_kit(user: User, job: Job) -> Kit | None:
    return Kit.objects.for_user(user).filter(job=job, status=Kit.Status.READY).first()


def expire_stuck(kits: list[Kit]) -> None:
    """A kit still queued or generating long after it started was lost (a worker restart, say).
    Mark it failed so the user can try again."""
    cutoff = timezone.now() - STUCK_AFTER
    for kit in kits:
        if kit.in_progress and kit.updated_at < cutoff:
            changed = Kit.objects.filter(
                pk=kit.pk, status=kit.status, updated_at__lt=cutoff
            ).update(
                status=Kit.Status.FAILED,
                progress="",
                error="This took too long and was stopped. Please try again.",
            )
            if changed:
                kit.status, kit.progress = Kit.Status.FAILED, ""
                kit.error = "This took too long and was stopped. Please try again."
                log_event(
                    logger, logging.WARNING, "kit_expired", "Stuck kit marked failed", kit_id=kit.pk
                )


def _default_track(user: User) -> Track | None:
    return Track.objects.for_user(user).filter(is_default=True).first() or (
        Track.objects.for_user(user).first()
    )


def has_profile_content(user: User) -> bool:
    return (
        Experience.objects.for_user(user).exists()
        or Project.objects.for_user(user).exists()
        or Skill.objects.for_user(user).exists()
    )


# --- requesting a kit ------------------------------------------------------------------------


def _enqueue(kit_id: int) -> None:
    from apps.core.tasks import enqueue

    from .tasks import generate_kit_task

    enqueue(generate_kit_task, kit_id)


def request_kit(
    user: User,
    job: Job,
    *,
    instruction: str = "",
    match: Match | None = None,
    auto: bool = False,
) -> Kit:
    """Create a queued kit (a new version) and hand it to the worker. If one is already being
    prepared for this job, that one is returned instead. Raises `NotOwner` if the job is not
    visible to the user, `EmptyProfile`, or `QuotaExceeded` before anything is queued."""
    if not Job.objects.visible_to(user).filter(pk=job.pk).exists():
        raise NotOwner("Job not found.")
    in_flight = Kit.objects.for_user(user).filter(
        job=job, status__in=[Kit.Status.QUEUED, Kit.Status.GENERATING]
    )
    expire_stuck(list(in_flight))
    active = in_flight.all().first()  # a fresh query: expiry may have changed the rows
    if active is not None:
        return active
    if not has_profile_content(user):
        raise EmptyProfile("Add your experience or skills to your profile first.")
    check_quota(user, LLMCall.Task.GENERATE_CV)  # raises QuotaExceeded

    if match is None:
        match = Match.objects.for_user(user).filter(job=job).first()
    track = (match.track if match else None) or _default_track(user)
    profile = Profile.objects.filter(user=user).first()
    instruction = " ".join(instruction.split())[:MAX_INSTRUCTION_CHARS]

    kit = _create_version(
        user=user,
        job=job,
        match=match,
        track=track,
        instruction=instruction,
        auto_generated=auto,
        profile_version=profile.version if profile else 1,
    )
    transaction.on_commit(functools.partial(_enqueue, kit.pk))
    log_event(
        logger, logging.INFO, "kit_requested", "Kit requested",
        kit_id=kit.pk, job_id=job.pk, version=kit.version, auto=auto,
        with_instruction=bool(instruction),
    )  # fmt: skip
    return kit


def _create_version(*, user: User, job: Job, **fields: Any) -> Kit:
    for _ in range(3):
        latest = Kit.objects.for_user(user).filter(job=job).order_by("-version").first()
        version = (latest.version if latest else 0) + 1
        try:
            with transaction.atomic():
                return Kit.objects.create(
                    user=user, job=job, version=version, status=Kit.Status.QUEUED,
                    progress="Waiting for a worker…", **fields,
                )  # fmt: skip
        except IntegrityError:  # two requests raced for the same version number
            continue
    raise KitInProgress("A kit for this job is already being prepared.")


def auto_generate_if_eligible(match: Match) -> Kit | None:
    """After scoring (spec §10.4): start a kit when the score reaches the user's auto-kit
    threshold, the allowance has room and no kit exists yet. Never raises."""
    try:
        user, job = match.user, match.job
        threshold = (
            UserSettings.objects.for_user(user).values_list("auto_kit_threshold", flat=True).first()
        )
        if threshold is None or match.score is None or match.score < threshold:
            return None
        if match.status in (Match.Status.DISMISSED, Match.Status.APPLIED):
            return None
        if Kit.objects.for_user(user).filter(job=job).exists():
            return None
        if kits_exhausted(user) or not has_profile_content(user):
            return None
        return request_kit(user, job, match=match, auto=True)
    except (QuotaExceeded, EmptyProfile, NotOwner, KitInProgress):
        return None


# --- prompt inputs ---------------------------------------------------------------------------


def _profile_block(profile: dict[str, Any]) -> str:
    """The profile as the model sees it: the whole item tree with IDs, minus contact details."""
    basics = {k: v for k, v in profile["basics"].items() if k in ("headline", "summary")}
    basics["years_experience"] = profile["basics"].get("years_experience")
    slim = {**profile, "basics": basics}
    slim.pop("version", None)
    return wrap_untrusted(
        "profile", json.dumps(slim, ensure_ascii=False, separators=(",", ":")), PROFILE_PROMPT_CHARS
    )


def build_job_block(job: Job) -> str:
    facts = [f"Title: {job.title}", f"Company: {job.company}", f"Work type: {job.work_type}"]
    if job.pay_text:
        facts.append(f"Pay: {job.pay_text}")
    if job.locations_raw:
        facts.append("Open to: " + ", ".join(str(x) for x in job.locations_raw))
    extracted = job.extracted or {}
    for label, key in (("Seniority", "seniority"), ("Hours", "hours_commitment")):
        if extracted.get(key):
            facts.append(f"{label}: {extracted[key]}")
    for label, key in (
        ("Required skills", "required_skills"),
        ("Nice to have", "nice_to_have_skills"),
        ("Responsibilities", "responsibilities"),
        ("Requirements", "requirements"),
    ):
        values = extracted.get(key)
        if isinstance(values, list) and values:
            facts.append(f"{label}: " + "; ".join(str(v) for v in values))
    facts.append("\nDescription:\n" + job.description_text[:JOB_PROMPT_CHARS])
    return wrap_untrusted("job_posting", "\n".join(facts), JOB_PROMPT_CHARS + 2000)


def _job_skills(job: Job) -> list[str]:
    extracted = job.extracted or {}
    skills: list[str] = []
    for key in ("required_skills", "nice_to_have_skills"):
        values = extracted.get(key)
        if isinstance(values, list):
            skills += [str(v) for v in values if isinstance(v, str)]
    return skills


def _track_text(track: Track | None) -> str:
    if track is None:
        return "none"
    parts = [f"{track.name}"]
    if track.target_titles:
        parts.append("target roles: " + ", ".join(track.target_titles))
    if track.min_pay_amount is not None:
        pay = f"{track.min_pay_amount} {track.min_pay_currency} per {track.min_pay_period}"
        parts.append(f"minimum pay: {pay}")
    return "; ".join(parts)


def _emphasis(track: Track | None) -> str:
    return (track.cv_emphasis.strip() if track and track.cv_emphasis else "") or "none"


def _instruction_block(kit: Kit) -> str:
    if not kit.instruction:
        return "none"
    return wrap_untrusted("user_instruction", kit.instruction, MAX_INSTRUCTION_CHARS)


def cv_as_text(cv: CVData) -> str:
    lines = [f"Headline: {cv.headline}", f"Summary: {cv.summary}"]
    for exp in cv.experiences:
        lines += [f"- ({exp.experience_id}) {b.text}" for b in exp.bullets]
    for project in cv.projects:
        lines += [f"- ({project.project_id}) {b.text}" for b in project.bullets]
    return "\n".join(lines)


# --- fallbacks (the user's own words) --------------------------------------------------------


def _fallback_summary(profile: dict[str, Any]) -> str:
    """The profile summary, cut at a sentence boundary within the CV summary word limit."""
    words = str(profile["basics"].get("summary") or "").split()
    if len(words) <= validation.MAX_SUMMARY_WORDS:
        return " ".join(words)
    clipped = " ".join(words[: validation.MAX_SUMMARY_WORDS])
    cut = max(clipped.rfind(". "), clipped.rfind("."))
    return clipped[: cut + 1] if cut > 0 else ""


def _fallback_experiences(profile: dict[str, Any]) -> list[CVExperience]:
    """Every job with its bullets exactly as the user wrote them."""
    result = []
    for exp in profile["experiences"]:
        bullets = [
            CVBullet(text=b["text"], source_ids=[b["id"]])
            for b in exp["bullets"][: validation.MAX_EXPERIENCE_BULLETS]
            if b["text"].strip()
        ]
        result.append(CVExperience(experience_id=exp["id"], bullets=bullets))
    return result


def _fallback_skills(profile: dict[str, Any]) -> list[str]:
    ranked = sorted(
        profile["skills"],
        key=lambda s: (-_LEVEL_RANK.get(s["level"], 0), -(s["years"] or 0), s["name"].lower()),
    )
    return [s["id"] for s in ranked[: validation.MAX_SKILLS]]


# --- the pipeline ----------------------------------------------------------------------------


@dataclass
class _CVOutcome:
    cv: CVData
    report: dict[str, Any]


def _set_progress(kit: Kit, text: str) -> None:
    Kit.objects.filter(pk=kit.pk).update(progress=text, updated_at=timezone.now())


def _fail(kit_id: int, message: str) -> None:
    Kit.objects.filter(pk=kit_id).update(
        status=Kit.Status.FAILED, progress="", error=message[:300], updated_at=timezone.now()
    )


def _issue_feedback(issues: list[validation.Issue]) -> str:
    return " | ".join(f"{i.field}: {i.message}" for i in issues[:20]) or "none"


def _build_cv(kit: Kit, profile: dict[str, Any]) -> _CVOutcome:
    """Steps 1-3: write the CV, validate it, retry once with the problems, then repair or drop."""
    job_skills = _job_skills(kit.job)
    variables = {
        "job": build_job_block(kit.job),
        "emphasis": _emphasis(kit.track),
        "instruction": _instruction_block(kit),
        "profile": _profile_block(profile),
        "feedback": "none",
    }

    def write(feedback: str) -> CVData:
        return call_structured(
            task=LLMCall.Task.GENERATE_CV,
            prompt_name="generate_cv",
            variables={**variables, "feedback": feedback},
            schema=CVData,
            user=kit.user,
            max_output_tokens=3000,
        )

    cv = write("none")
    issues = validation.validate_cv(cv, profile, job_skills=job_skills)
    retried = False
    if issues:
        retried = True
        try:
            second = write(_issue_feedback(issues))
        except (QuotaExceeded, LLMError):
            second = None  # keep the first attempt and repair it below
        if second is not None:
            cv = second
            issues = validation.validate_cv(cv, profile, job_skills=job_skills)

    notes: list[str] = []
    repaired: validation.Repaired | None = None
    if issues:
        repaired = validation.drop_failing(
            cv,
            issues,
            fallback_headline=str(profile["basics"].get("headline") or ""),
            fallback_summary=_fallback_summary(profile),
        )
        cv = repaired.cv

    # An empty CV body means the model's choices were unusable: fall back to the user's own text.
    if not cv.experiences and not cv.projects and profile["experiences"]:
        cv.experiences = _fallback_experiences(profile)
        notes.append("Your own wording was used for your experience.")
    if not cv.skills and profile["skills"]:
        cv.skills = _fallback_skills(profile)
    if not cv.headline.strip():
        cv.headline = str(profile["basics"].get("headline") or "")
    if not cv.summary.strip():
        cv.summary = _fallback_summary(profile)

    # What is left after a repair is either clean or the user's own wording (a fallback), so the
    # report lists only what was repaired or removed.
    report = validation.build_report([], repaired, retried=retried, notes=notes)
    return _CVOutcome(cv=cv, report=report)


def _letter_feedback(issues: list[validation.Issue]) -> str:
    return " | ".join(f"The letter {i.message}" for i in issues) or "none"


def _build_letter(kit: Kit, cv: CVData, profile: dict[str, Any]) -> tuple[str, list[str]]:
    """Step 4: the cover letter. One retry on style, length or invented numbers; whatever is
    left is reported as a warning, never blocks, except em dashes which are repaired."""
    cv_text = cv_as_text(cv)
    allowed = validation.numbers_in(
        " ".join(
            [cv_text, kit.job.description_text, kit.job.title, kit.job.pay_text]
            + [str(x) for x in kit.job.locations_raw]
            + [json.dumps(profile.get("basics", {}))]
        )
    )
    allowed |= validation.numbers_in(json.dumps(profile.get("experiences", [])))
    variables = {
        "job": build_job_block(kit.job),
        "emphasis": _emphasis(kit.track),
        "instruction": _instruction_block(kit),
        "summary": wrap_untrusted("candidate", profile_summary(kit.user), 6000),
        "cv": wrap_untrusted("cv_content", cv_text, 8000),
        "feedback": "none",
    }

    def write(feedback: str) -> str:
        return call_text(
            task=LLMCall.Task.GENERATE_COVER_LETTER,
            prompt_name="generate_cover_letter",
            variables={**variables, "feedback": feedback},
            user=kit.user,
            max_output_tokens=1200,
        )

    text = write("none")
    issues = validation.check_letter(text, allowed_numbers=allowed)
    if issues:
        # On failure keep the first attempt; what is left is reported as warnings.
        with contextlib.suppress(QuotaExceeded, LLMError):
            text = write(_letter_feedback(issues))
    text = style.strip_em_dashes(text).strip()
    warnings = [
        f"Cover letter {i.message}" for i in validation.check_letter(text, allowed_numbers=allowed)
    ]
    return text, warnings


def _clean_source_ids(ids: list[str], known: set[str]) -> list[str]:
    return [i for i in ids if i in known]


def _build_interview(kit: Kit, profile: dict[str, Any]) -> list[dict[str, Any]]:
    known = set(validation.ProfileIndex(profile).kinds)
    gaps = kit.match.gaps if kit.match and kit.match.gaps else []
    prep = call_structured(
        task=LLMCall.Task.GENERATE_INTERVIEW,
        prompt_name="generate_interview",
        variables={
            "job": build_job_block(kit.job),
            "gaps": wrap_untrusted("match_gaps", "; ".join(str(g) for g in gaps) or "none", 1500),
            "profile": _profile_block(profile),
        },
        schema=InterviewPrep,
        user=kit.user,
        max_output_tokens=3500,
    )
    items: list[dict[str, Any]] = []
    for item in prep.items[:MAX_INTERVIEW_ITEMS]:
        if not item.question.strip():
            continue
        items.append(
            {
                "category": item.category,
                "question": style.strip_em_dashes(item.question),
                "why_they_ask": style.strip_em_dashes(item.why_they_ask),
                "answer_outline": [
                    style.strip_em_dashes(p) for p in item.answer_outline if p.strip()
                ][:4],
                "source_ids": _clean_source_ids(item.source_ids, known),
            }
        )
    return items


def _build_screening(kit: Kit, profile: dict[str, Any]) -> list[dict[str, str]]:
    result = call_structured(
        task=LLMCall.Task.GENERATE_SCREENING,
        prompt_name="generate_screening",
        variables={
            "job": build_job_block(kit.job),
            "track": _track_text(kit.track),
            "profile": _profile_block(profile),
        },
        schema=ScreeningSet,
        user=kit.user,
        max_output_tokens=1800,
    )
    return [
        {
            "question": style.strip_em_dashes(a.question).strip(),
            "answer": style.strip_em_dashes(a.answer).strip(),
        }
        for a in result.answers
        if a.question.strip() and a.answer.strip()
    ]


def generate_kit(kit_id: int) -> Kit | None:
    """Run the whole pipeline for a queued kit (spec §12.2). Idempotent: only a queued kit is
    picked up. Failures are recorded on the kit with a short message and logged once; the CV and
    cover letter are required, interview prep and screening answers are best-effort, and a
    rendering problem keeps the text so the user can re-render without spending anything."""
    claimed = Kit.objects.filter(pk=kit_id, status=Kit.Status.QUEUED).update(
        status=Kit.Status.GENERATING, progress=STEP_CV, updated_at=timezone.now()
    )
    if not claimed:
        return None
    kit = Kit.objects.select_related("job", "track", "match", "user").get(pk=kit_id)
    try:
        return _run_pipeline(kit)
    except QuotaExceeded as exc:
        _fail(kit.pk, exc.message)
        log_event(logger, logging.WARNING, "kit_blocked", "Kit blocked by quota", kit_id=kit.pk)
    except EmptyProfile as exc:
        _fail(kit.pk, exc.message)
    except LLMError as exc:
        _fail(kit.pk, "We could not write your documents just now. Please try again.")
        log_event(
            logger, logging.ERROR, "kit_failed", "Kit generation failed",
            kit_id=kit.pk, reason=type(exc).__name__, exc_info=True,
        )  # fmt: skip
    except Exception:
        _fail(kit.pk, "Something went wrong while preparing your documents. Please try again.")
        log_event(
            logger, logging.ERROR, "kit_failed", "Kit generation failed",
            kit_id=kit.pk, reason="unexpected", exc_info=True,
        )  # fmt: skip
    return None


def _run_pipeline(kit: Kit) -> Kit:
    profile = serialize_profile(kit.user)
    if not (profile["experiences"] or profile["projects"] or profile["skills"]):
        raise EmptyProfile("Add your experience or skills to your profile first.")

    outcome = _build_cv(kit, profile)
    cv, report = outcome.cv, outcome.report
    notes: list[str] = list(report.get("notes", []))

    _set_progress(kit, STEP_LETTER)
    letter, letter_warnings = _build_letter(kit, cv, profile)

    _set_progress(kit, STEP_INTERVIEW)
    interview: list[dict[str, Any]] = []
    try:
        interview = _build_interview(kit, profile)
    except (QuotaExceeded, LLMError):
        notes.append("Interview questions could not be prepared this time.")

    _set_progress(kit, STEP_SCREENING)
    screening: list[dict[str, str]] = []
    try:
        screening = _build_screening(kit, profile)
    except (QuotaExceeded, LLMError):
        notes.append("Screening answers could not be prepared this time.")

    report["notes"] = notes
    report["letter_warnings"] = letter_warnings
    kit.cv_data = cv.model_dump()
    kit.cover_letter_text = letter
    kit.interview_qa = interview
    kit.screening_answers = screening
    kit.validation_report = report
    kit.profile_version = int(profile.get("version", 1))
    kit.save()

    _set_progress(kit, STEP_RENDER)
    render_error = _store_documents(kit)

    kit.status = Kit.Status.READY
    kit.progress = ""
    kit.error = render_error
    kit.save()
    log_event(
        logger, logging.INFO, "kit_generated", "Kit generated",
        kit_id=kit.pk, job_id=kit.job_id, version=kit.version,
        validation_passed=bool(report.get("passed")), issues=len(report.get("issues", [])),
        interview_items=len(interview), screening_answers=len(screening),
        documents=bool(not render_error),
    )  # fmt: skip
    hooks.after_generate(kit)
    return kit


# --- documents -------------------------------------------------------------------------------


def _store_documents(kit: Kit) -> str:
    """Render and save the four files. Returns "" or a short user-safe message if any failed;
    files that rendered are saved either way."""
    files, error = documents.render_all(kit)
    if error is not None:
        log_event(
            logger, logging.ERROR, "kit_render_failed", "Kit rendering failed",
            kit_id=kit.pk, rendered=len(files), exc_info=error,
        )  # fmt: skip
    for key in FILE_KEYS:
        if key not in files:
            continue
        field = getattr(kit, key)
        if field:
            field.delete(save=False)
        extension = "pdf" if key.endswith("pdf") else "docx"
        getattr(kit, key).save(f"{key}.{extension}", ContentFile(files[key]), save=False)
    kit.save()
    if error is None:
        return ""
    return f"{error.message} Your text is saved; try “Re-render documents”."


def rerender(user: User, kit: Kit) -> Kit:
    """Re-render the files from the stored text. No LLM call, no quota."""
    _require_ready(user, kit)
    error = _store_documents(kit)
    kit.error = error
    kit.save()
    log_event(logger, logging.INFO, "kit_rerendered", "Kit re-rendered", kit_id=kit.pk)
    return kit


def _require_ready(user: User, kit: Kit) -> None:
    if kit.user_id != user.pk:
        raise NotOwner("Kit not found.")
    if not kit.is_ready:
        raise KitTooOld("This kit is not ready to edit.")


@dataclass
class KitEdits:
    headline: str | None = None
    summary: str | None = None
    cover_letter: str | None = None
    # (section, entry index, bullet index) -> new text; an empty text removes the bullet
    bullets: dict[tuple[str, int, int], str] | None = None


def apply_edits(user: User, kit: Kit, edits: KitEdits) -> list[str]:
    """Save the user's edits and re-render without calling the model (spec §12.6).

    Edited text goes through the style check, but only as warnings (returned and stored). The
    bullets keep their original `source_ids`.
    """
    _require_ready(user, kit)
    cv = CVData.model_validate(kit.cv_data)
    checks: list[tuple[str, str]] = []

    if edits.headline is not None:
        cv.headline = _one_line(edits.headline)
        checks.append(("headline", cv.headline))
    if edits.summary is not None:
        cv.summary = " ".join(edits.summary.split())
        checks.append(("summary", cv.summary))
    for (section, i, j), text in sorted((edits.bullets or {}).items(), reverse=True):
        entries = cv.experiences if section == "exp" else cv.projects
        if i >= len(entries) or j >= len(entries[i].bullets):
            continue
        cleaned = " ".join(text.split())
        if cleaned:
            entries[i].bullets[j].text = cleaned
            checks.append((f"{section}[{i}].bullets[{j}]", cleaned))
        else:
            del entries[i].bullets[j]
    if edits.cover_letter is not None:
        kit.cover_letter_text = _normalise_letter(edits.cover_letter)
        checks.append(("cover_letter", kit.cover_letter_text))

    warnings = [f"{i.field}: {i.message}" for i in validation.check_edit(checks)]
    kit.cv_data = cv.model_dump()
    kit.user_edited = True
    report = dict(kit.validation_report or {})
    report["edit_warnings"] = warnings
    kit.validation_report = report
    kit.save()
    error = _store_documents(kit)
    kit.error = error
    kit.save()
    log_event(
        logger, logging.INFO, "kit_edited", "Kit edited", kit_id=kit.pk, warnings=len(warnings)
    )
    return warnings


def _one_line(text: str) -> str:
    return " ".join(text.split())[: validation.MAX_HEADLINE_CHARS]


def _normalise_letter(text: str) -> str:
    paragraphs = documents.split_paragraphs(text.replace("\r\n", "\n"))
    return "\n\n".join(" ".join(p.split()) for p in paragraphs)
