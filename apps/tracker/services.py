"""The application tracker: applications, stage changes, interviews, reminders (§14, §13.5).

Views, the Telegram bot and Huey tasks are thin wrappers over this module. Every function takes
the acting user and only touches that user's rows. Nothing here logs job, company or note text.
"""

import datetime
import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User, UserSettings
from apps.core.logging import log_event
from apps.jobs.models import Job
from apps.kits import services as kit_services
from apps.kits.models import Kit
from apps.matching import services as matching_services
from apps.matching.models import Match

from .exceptions import InvalidKit, InvalidStage, NotOwner
from .models import Application, ApplicationEvent, Interview, Stage

logger = logging.getLogger(__name__)

FOLLOWUP_AFTER = datetime.timedelta(days=7)
GHOST_SUGGESTION_AFTER = datetime.timedelta(days=21)
VALID_STAGES = {s.value for s in Stage}


# --- lookups ---------------------------------------------------------------------------------


def get_application(user: User, application_id: int) -> Application:
    application = (
        Application.objects.for_user(user)
        .select_related("job", "job__source", "kit", "track")
        .filter(pk=application_id)
        .first()
    )
    if application is None:
        raise NotOwner("Application not found.")
    return application


def get_interview(user: User, interview_id: int) -> Interview:
    interview = (
        Interview.objects.for_user(user)
        .select_related("application")
        .filter(pk=interview_id)
        .first()
    )
    if interview is None:
        raise NotOwner("Interview not found.")
    return interview


def application_for_job(user: User, job: Job) -> Application | None:
    return Application.objects.for_user(user).filter(job=job).first()


# --- creating --------------------------------------------------------------------------------


def _record(
    application: Application, from_stage: str, to_stage: str, at: datetime.datetime, note: str = ""
) -> None:
    ApplicationEvent.objects.create(
        user=application.user, application=application, from_stage=from_stage,
        to_stage=to_stage, at=at, note=note[:500],
    )  # fmt: skip


def apply_to_job(
    user: User,
    job: Job,
    *,
    match: Match | None = None,
    applied_at: datetime.datetime | None = None,
    channel: str = "",
) -> tuple[Application, bool]:
    """Mark a job applied (spec §14). Returns `(application, created)`.

    One application per user and job: applying again returns the existing one, except that a
    `saved` application moves to `applied`. The latest ready kit is linked, the match (if any)
    becomes `applied`, and the first follow-up is set 7 days out.
    """
    if not Job.objects.visible_to(user).filter(pk=job.pk).exists():
        raise NotOwner("Job not found.")
    now = applied_at or timezone.now()
    match = match or Match.objects.for_user(user).filter(job=job).first()
    if match is not None and match.user_id != user.pk:
        raise NotOwner("Match not found.")

    with transaction.atomic():
        existing = Application.objects.for_user(user).select_for_update().filter(job=job).first()
        if existing is not None:
            if existing.stage == Stage.SAVED:
                _move(existing, Stage.APPLIED, now, "")
                existing.channel = existing.channel or channel
                existing.save()
            application, created = existing, False
        else:
            kit = kit_services.latest_ready_kit(user, job)
            try:
                with transaction.atomic():
                    application = Application.objects.create(
                        user=user, job=job, kit=kit, track=match.track if match else None,
                        stage=Stage.APPLIED, applied_at=now, last_stage_change_at=now,
                        next_followup_at=now + FOLLOWUP_AFTER, channel=channel[:120],
                    )  # fmt: skip
            except IntegrityError:  # a simultaneous request created it first
                return Application.objects.for_user(user).get(job=job), False
            _record(application, "", Stage.APPLIED, now)
            created = True
        if match is not None:
            matching_services.set_status(user, match, Match.Status.APPLIED)
    log_event(
        logger, logging.INFO, "application_created" if created else "application_exists",
        "Application created" if created else "Application already existed",
        application_id=application.pk, job_id=job.pk,
    )  # fmt: skip
    return application, created


def add_manual_application(
    user: User,
    *,
    title: str,
    company: str,
    url: str = "",
    applied_on: datetime.date | None = None,
    channel: str = "",
    notes: str = "",
) -> Application:
    """An application for a job found outside the system: a private job holds the details."""
    now = timezone.now()
    when = (
        datetime.datetime.combine(applied_on, datetime.time(12, 0), tzinfo=datetime.UTC)
        if applied_on
        else now
    )
    job = Job.objects.create(
        origin=Job.Origin.MANUAL_TEXT, visibility=Job.Visibility.PRIVATE, owner=user,
        title=title.strip()[:250], company=company.strip()[:200], url=url.strip()[:500],
        first_seen_at=now, last_seen_at=now, raw={"manual_application": True},
    )  # fmt: skip
    application, _ = apply_to_job(user, job, applied_at=when, channel=channel)
    if notes.strip():
        application.notes = notes.strip()[:5000]
        application.save(update_fields=["notes", "updated_at"])
    return application


# --- stage changes -----------------------------------------------------------------------------


def _move(application: Application, to_stage: str, now: datetime.datetime, note: str) -> None:
    """Set the stage, write the event and fix the dates. The caller saves the application."""
    from_stage = application.stage
    application.stage = to_stage
    application.last_stage_change_at = now
    if to_stage == Stage.APPLIED:
        application.applied_at = application.applied_at or now
        application.next_followup_at = now + FOLLOWUP_AFTER
    else:
        application.next_followup_at = None  # reminders are only for applications awaiting a reply
    application.save()
    _record(application, from_stage, to_stage, now, note)


def change_stage(
    user: User,
    application: Application,
    to_stage: str,
    note: str = "",
    *,
    now: datetime.datetime | None = None,
) -> Application:
    """Move an application to another stage and record why (spec §14). Moving to the stage it is
    already in does nothing. Moving to `applied` also marks the match applied."""
    if application.user_id != user.pk:
        raise NotOwner("Application not found.")
    if to_stage not in VALID_STAGES:
        raise InvalidStage("That is not a stage.")
    if to_stage == application.stage:
        return application
    now = now or timezone.now()
    with transaction.atomic():
        _move(application, to_stage, now, note)
        if to_stage == Stage.APPLIED:
            match = Match.objects.for_user(user).filter(job_id=application.job_id).first()
            if match is not None:
                matching_services.set_status(user, match, Match.Status.APPLIED)
    log_event(
        logger, logging.INFO, "application_stage_changed", "Application stage changed",
        application_id=application.pk, to_stage=to_stage,
    )  # fmt: skip
    return application


# --- details -------------------------------------------------------------------------------------


def update_details(
    user: User, application: Application, *, notes: str | None = None, channel: str | None = None,
    outcome_reason: str | None = None, salary_offered: str | None = None,
) -> Application:  # fmt: skip
    if application.user_id != user.pk:
        raise NotOwner("Application not found.")
    if notes is not None:
        application.notes = notes.strip()[:5000]
    if channel is not None:
        application.channel = channel.strip()[:120]
    if outcome_reason is not None:
        application.outcome_reason = outcome_reason.strip()[:300]
    if salary_offered is not None:
        application.salary_offered = salary_offered.strip()[:120]
    application.save()
    return application


def set_kit(user: User, application: Application, kit_id: int | None) -> Application:
    """Choose which kit version was used. It must be a ready kit of the user's, for this job."""
    if application.user_id != user.pk:
        raise NotOwner("Application not found.")
    if kit_id is None:
        application.kit = None
    else:
        kit = Kit.objects.for_user(user).filter(
            pk=kit_id, job_id=application.job_id, status=Kit.Status.READY
        ).first()  # fmt: skip
        if kit is None:
            raise InvalidKit("That kit is not available for this job.")
        application.kit = kit
    application.save(update_fields=["kit", "updated_at"])
    return application


def ready_kits(user: User, application: Application) -> list[Kit]:
    return list(
        Kit.objects.for_user(user)
        .filter(job_id=application.job_id, status=Kit.Status.READY)
        .order_by("-version")
    )


def add_interview(user: User, application: Application, **fields: Any) -> Interview:
    if application.user_id != user.pk:
        raise NotOwner("Application not found.")
    return Interview.objects.create(user=user, application=application, **fields)


def update_interview(user: User, interview: Interview, **fields: Any) -> Interview:
    if interview.user_id != user.pk:
        raise NotOwner("Interview not found.")
    for name, value in fields.items():
        setattr(interview, name, value)
    interview.save()
    return interview


def delete_interview(user: User, interview: Interview) -> None:
    if interview.user_id != user.pk:
        raise NotOwner("Interview not found.")
    interview.delete()


def suggests_ghosted(application: Application, now: datetime.datetime | None = None) -> bool:
    """Applied, and no change for 21 days (spec §13.5)."""
    now = now or timezone.now()
    return (
        application.stage == Stage.APPLIED
        and now - application.last_stage_change_at >= GHOST_SUGGESTION_AFTER
    )


def board(user: User) -> dict[str, list[Application]]:
    """Every stage mapped to the user's applications in it, most recently changed first."""
    columns: dict[str, list[Application]] = {s.value: [] for s in Stage}
    for application in Application.objects.for_user(user).select_related(
        "job", "job__source", "kit"
    ):
        columns[application.stage].append(application)
    return columns


def stage_counts(user: User) -> dict[str, int]:
    counts = {s.value: 0 for s in Stage}
    for stage in Application.objects.for_user(user).values_list("stage", flat=True):
        counts[stage] += 1
    return counts


# --- follow-up reminders (spec §13.5) ----------------------------------------


def send_followup_reminders(now: datetime.datetime | None = None) -> dict[str, int]:
    """Daily: one reminder for each application whose follow-up date has passed, then the next
    follow-up is 7 days away. An application with no change for 21 days also gets one
    "consider marking it ghosted" suggestion. Idempotent: notifications are keyed by application
    and date, so a second run the same day adds nothing."""
    from apps.notifications import schedule
    from apps.notifications import services as notification_services
    from apps.notifications.models import Notification

    now = now or timezone.now()
    counts = {"reminders": 0, "ghost_suggestions": 0}

    def queue(application: Application, key: str, what: str) -> bool:
        user = application.user
        settings_row = UserSettings.objects.filter(user=user).first()
        if settings_row is None or settings_row.notify_mode == UserSettings.NotifyMode.OFF:
            return False
        deliver_after = schedule.quiet_hours_end(
            now, settings_row.timezone, settings_row.quiet_hours_start, settings_row.quiet_hours_end
        )
        queued = False
        for channel in notification_services.channels_for(user, settings_row):
            _, created = notification_services.create_notification(
                user, kind=Notification.Kind.FOLLOWUP, channel=channel,
                dedupe_key=f"{key}:{channel}", deliver_after=deliver_after,
                payload={"application_id": application.pk, "what": what},
            )  # fmt: skip
            queued = queued or created
        return queued

    due = Application.objects.filter(
        stage=Stage.APPLIED, next_followup_at__isnull=False, next_followup_at__lte=now,
        user__is_active=True,
    ).select_related("user")  # fmt: skip
    for application in due:
        day = (
            application.next_followup_at.date().isoformat() if application.next_followup_at else ""
        )
        queue(application, f"followup:{application.pk}:{day}", "reminder")
        Application.objects.filter(pk=application.pk).update(next_followup_at=now + FOLLOWUP_AFTER)
        counts["reminders"] += 1

    stale = Application.objects.filter(
        stage=Stage.APPLIED, last_stage_change_at__lte=now - GHOST_SUGGESTION_AFTER,
        user__is_active=True,
    ).select_related("user")  # fmt: skip
    for application in stale:
        if queue(application, f"ghost:{application.pk}", "ghost"):
            counts["ghost_suggestions"] += 1
    log_event(logger, logging.INFO, "followups_queued", "Follow-ups queued", **counts)
    return counts
