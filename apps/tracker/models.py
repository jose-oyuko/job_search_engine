"""Applications, their stage history and interviews (spec §5.8)."""

from django.db import models

from apps.core.models import TimeStampedModel, UserOwnedModel
from apps.jobs.models import Job
from apps.kits.models import Kit
from apps.profiles.models import Track


class Stage(models.TextChoices):
    SAVED = "saved", "Saved"
    APPLIED = "applied", "Applied"
    SCREENING = "screening", "Screening"
    INTERVIEW = "interview", "Interview"
    OFFER = "offer", "Offer"
    HIRED = "hired", "Hired"
    REJECTED = "rejected", "Rejected"
    WITHDRAWN = "withdrawn", "Withdrawn"
    GHOSTED = "ghosted", "Ghosted"


# Reaching one of these means the employer answered (spec §14: "anything past applied").
RESPONSE_STAGES = (Stage.SCREENING, Stage.INTERVIEW, Stage.OFFER, Stage.HIRED, Stage.REJECTED)
BOARD_STAGES = [s.value for s in Stage]


class Application(UserOwnedModel, TimeStampedModel):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    kit = models.ForeignKey(
        Kit, null=True, blank=True, on_delete=models.SET_NULL, related_name="applications"
    )
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    stage = models.CharField(max_length=10, choices=Stage.choices, default=Stage.APPLIED)
    applied_at = models.DateTimeField(null=True, blank=True)
    last_stage_change_at = models.DateTimeField()
    next_followup_at = models.DateTimeField(null=True, blank=True)
    # Where they applied: a platform, an email address, a referral...
    channel = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True, max_length=5000)
    outcome_reason = models.CharField(max_length=300, blank=True)
    salary_offered = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-last_stage_change_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "job"], name="application_unique_user_job"),
        ]
        indexes = [
            models.Index(fields=["user", "stage"]),
            models.Index(fields=["stage", "next_followup_at"]),
        ]

    def __str__(self) -> str:
        return f"Application #{self.pk}"


class ApplicationEvent(UserOwnedModel):
    """One stage change. Never edited, so the history stays trustworthy."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="events")
    from_stage = models.CharField(max_length=10, blank=True)
    to_stage = models.CharField(max_length=10)
    at = models.DateTimeField()
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self) -> str:
        return f"{self.from_stage or '-'} to {self.to_stage}"


class Interview(UserOwnedModel, TimeStampedModel):
    class Kind(models.TextChoices):
        AI_SCREEN = "ai_screen", "AI screening"
        PHONE = "phone", "Phone"
        TECHNICAL = "technical", "Technical"
        PANEL = "panel", "Panel"
        FINAL = "final", "Final"
        OTHER = "other", "Other"

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="interviews"
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    notes = models.TextField(blank=True, max_length=3000)
    questions_asked = models.TextField(blank=True, max_length=3000)
    outcome = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["scheduled_at", "id"]

    def __str__(self) -> str:
        return f"Interview #{self.pk}"
