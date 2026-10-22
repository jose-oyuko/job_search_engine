"""The shared job pool (spec §5.5). Jobs are global; only private jobs belong to a user."""

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel, UserOwnedModel
from apps.sources.models import Source


class JobQuerySet(models.QuerySet["Job"]):
    def visible_to(self, user: object) -> "JobQuerySet":
        """Public jobs, plus the user's own private jobs (spec §16). Anonymous users see none."""
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(Q(visibility=Job.Visibility.PUBLIC) | Q(owner=user))

    def canonical(self) -> "JobQuerySet":
        """Jobs that are not duplicates of another job; only these are matched or notified."""
        return self.filter(duplicate_of__isnull=True)

    def active(self) -> "JobQuerySet":
        return self.filter(status=Job.Status.ACTIVE)


class Job(TimeStampedModel):
    class Origin(models.TextChoices):
        SCRAPED = "scraped", "Scraped"
        MANUAL_URL = "manual_url", "Added by URL"
        MANUAL_TEXT = "manual_text", "Added by pasted text"
        EMAIL = "email", "From a job-alert email"

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        PRIVATE = "private", "Private"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        UNKNOWN = "unknown", "Unknown"

    class PayPeriod(models.TextChoices):
        HOUR = "hour", "per hour"
        TASK = "task", "per task"
        DAY = "day", "per day"
        WEEK = "week", "per week"
        MONTH = "month", "per month"
        YEAR = "year", "per year"
        ONE_TIME = "one_time", "one-time"
        UNKNOWN = "unknown", "unknown"

    class WorkType(models.TextChoices):
        REMOTE = "remote", "Remote"
        ONSITE = "onsite", "On-site"
        HYBRID = "hybrid", "Hybrid"
        UNKNOWN = "unknown", "Unknown"

    source = models.ForeignKey(
        Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )
    external_id = models.CharField(max_length=200, blank=True)
    origin = models.CharField(max_length=12, choices=Origin.choices, default=Origin.SCRAPED)
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.PUBLIC
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
        help_text="Set for private jobs; deleting the user deletes their private jobs.",
    )

    url = models.URLField(max_length=500, blank=True)
    apply_url = models.URLField(max_length=1000, blank=True)
    alternate_urls = models.JSONField(default=list, blank=True)

    title = models.CharField(max_length=250)
    company = models.CharField(max_length=200)
    company_platform = models.CharField(max_length=120, blank=True)
    description_html = models.TextField(blank=True)
    description_text = models.TextField(blank=True)

    pay_text = models.CharField(max_length=300, blank=True)
    pay_min = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    pay_max = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    pay_currency = models.CharField(max_length=3, blank=True)
    pay_period = models.CharField(
        max_length=10, choices=PayPeriod.choices, default=PayPeriod.UNKNOWN
    )

    locations_raw = models.JSONField(default=list, blank=True)
    eligible_countries = models.JSONField(default=list, blank=True)
    eligible_regions = models.JSONField(default=list, blank=True)
    is_global = models.BooleanField(default=False)
    eligibility_known = models.BooleanField(default=False)

    work_type = models.CharField(max_length=8, choices=WorkType.choices, default=WorkType.UNKNOWN)
    employment_type = models.CharField(max_length=30, blank=True)

    posted_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)

    dedupe_key = models.CharField(max_length=500, blank=True, db_index=True)
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates"
    )

    extracted = models.JSONField(null=True, blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    scam_flags = models.JSONField(default=list, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    objects = JobQuerySet.as_manager()

    class Meta:
        ordering = ["-first_seen_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=Q(source__isnull=False),
                name="job_unique_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "-first_seen_at"]),
            models.Index(fields=["source", "status", "last_seen_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} at {self.company}"


class JobReport(UserOwnedModel, TimeStampedModel):
    """A user's "report this listing" (scam, expired, wrong info). The UI arrives in Phase 6."""

    class Reason(models.TextChoices):
        SCAM = "scam", "Looks like a scam"
        EXPIRED = "expired", "No longer available"
        WRONG_INFO = "wrong_info", "Wrong information"
        OTHER = "other", "Other"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="reports")
    reason = models.CharField(max_length=12, choices=Reason.choices)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Report #{self.pk} ({self.reason})"
