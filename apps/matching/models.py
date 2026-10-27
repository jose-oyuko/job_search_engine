"""Per-user matches between a user and a job (spec §5.6)."""

from django.db import models

from apps.core.models import TimeStampedModel, UserOwnedModel
from apps.jobs.models import Job
from apps.profiles.models import Track


class Match(UserOwnedModel, TimeStampedModel):
    class Verdict(models.TextChoices):
        STRONG = "strong", "Strong match"
        GOOD = "good", "Good match"
        WEAK = "weak", "Weak match"
        POOR = "poor", "Poor match"

    class Status(models.TextChoices):
        NEW = "new", "New"
        NOTIFIED = "notified", "Notified"
        SEEN = "seen", "Seen"
        SAVED = "saved", "Saved"
        DISMISSED = "dismissed", "Dismissed"
        APPLIED = "applied", "Applied"

    class DismissReason(models.TextChoices):
        NOT_RELEVANT = "not_relevant", "Not relevant"
        PAY_TOO_LOW = "pay_too_low", "Pay too low"
        LOCATION = "location", "Location"
        SENIORITY = "seniority", "Seniority"
        COMPANY = "company", "Company"
        OTHER = "other", "Other"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="matches")
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # The user put this job in front of the rules themselves (added it by hand or forwarded it by
    # email), so the rule filter never removes it, even if the job does not fit their targets.
    user_added = models.BooleanField(default=False)
    rule_pass = models.BooleanField(default=True)
    rule_reasons = models.JSONField(default=list, blank=True)

    score = models.PositiveSmallIntegerField(null=True, blank=True)
    verdict = models.CharField(max_length=6, choices=Verdict.choices, blank=True)
    strengths = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    summary = models.CharField(max_length=300, blank=True)
    # Profile version the score was computed from; if it differs from the current one, it is stale.
    scored_profile_version = models.PositiveIntegerField(null=True, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True)
    llm_model = models.CharField(max_length=100, blank=True)
    # Set when a scoring task is queued, so viewing a match does not queue it again and again.
    scoring_queued_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    dismiss_reason = models.CharField(max_length=14, choices=DismissReason.choices, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "job"], name="match_unique_user_job"),
        ]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["user", "-score"]),
        ]
        verbose_name_plural = "matches"

    def is_stale(self, current_profile_version: int) -> bool:
        """A score computed from an older version of the profile."""
        return self.score is not None and self.scored_profile_version != current_profile_version

    def __str__(self) -> str:
        return f"Match #{self.pk}"
