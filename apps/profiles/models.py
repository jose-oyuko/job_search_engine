"""The user's master profile (spec §5.3). Every row is owned by exactly one user.

`Profile.version` is bumped by `services` on any change, and is what later phases use to
invalidate scores and generated kits. Primary keys are the stable IDs the kit generator cites.
"""

import uuid
from pathlib import PurePath

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import TimeStampedModel, UserOwnedModel, UserOwnedQuerySet
from apps.core.storage import private_storage


class EmploymentType(models.TextChoices):
    FULL_TIME = "full_time", "Full-time"
    PART_TIME = "part_time", "Part-time"
    CONTRACT = "contract", "Contract"
    FREELANCE = "freelance", "Freelance"
    GIG = "gig", "Gig / task-based"


class WorkType(models.TextChoices):
    REMOTE = "remote", "Remote"
    ONSITE = "onsite", "On-site"
    HYBRID = "hybrid", "Hybrid"


class Profile(TimeStampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    headline = models.CharField(max_length=160, blank=True)
    summary = models.TextField(max_length=3000, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    public_email = models.EmailField(blank=True)
    location_text = models.CharField(max_length=120, blank=True)
    links = models.JSONField(default=list, blank=True, help_text="List of {label, url}.")
    years_experience = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    objects = UserOwnedQuerySet.as_manager()

    def __str__(self) -> str:
        return f"Profile of user {self.user_id}"


class Skill(UserOwnedModel, TimeStampedModel):
    class Category(models.TextChoices):
        TECHNICAL = "technical", "Technical"
        TOOL = "tool", "Tool"
        SOFT = "soft", "Soft skill"
        LANGUAGE = "language", "Language"
        DOMAIN = "domain", "Domain knowledge"

    class Level(models.TextChoices):
        BASIC = "basic", "Basic"
        INTERMEDIATE = "intermediate", "Intermediate"
        ADVANCED = "advanced", "Advanced"
        EXPERT = "expert", "Expert"

    name = models.CharField(max_length=100)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.TECHNICAL)
    level = models.CharField(max_length=15, choices=Level.choices, blank=True)
    years = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)

    class Meta:
        ordering = ["category", "name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), "user", name="skill_unique_name_per_user"),
        ]

    def __str__(self) -> str:
        return self.name


class Track(UserOwnedModel, TimeStampedModel):
    """A target career path. Exactly one track per user is the default (enforced in the DB)."""

    class PayPeriod(models.TextChoices):
        HOUR = "hour", "per hour"
        MONTH = "month", "per month"
        YEAR = "year", "per year"

    name = models.CharField(max_length=100)
    is_default = models.BooleanField(default=False)
    target_titles = models.JSONField(default=list, blank=True)
    include_keywords = models.JSONField(default=list, blank=True)
    exclude_keywords = models.JSONField(default=list, blank=True)
    work_types = models.JSONField(default=list, blank=True)
    locations = models.JSONField(default=list, blank=True)
    min_pay_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    min_pay_currency = models.CharField(max_length=3, default="USD")
    min_pay_period = models.CharField(
        max_length=5, choices=PayPeriod.choices, default=PayPeriod.HOUR
    )
    employment_types = models.JSONField(default=list, blank=True)
    excluded_companies = models.JSONField(default=list, blank=True)
    cv_emphasis = models.TextField(max_length=1000, blank=True)

    class Meta:
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"], condition=models.Q(is_default=True), name="track_one_default"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Experience(UserOwnedModel, TimeStampedModel):
    title = models.CharField(max_length=150)
    company = models.CharField(max_length=150)
    location = models.CharField(max_length=120, blank=True)
    employment_type = models.CharField(max_length=10, choices=EmploymentType.choices, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True, help_text="Empty means current.")
    description = models.TextField(max_length=3000, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return f"{self.title} at {self.company}"


class ExperienceBullet(UserOwnedModel, TimeStampedModel):
    experience = models.ForeignKey(Experience, on_delete=models.CASCADE, related_name="bullets")
    text = models.CharField(max_length=500)
    metrics = models.CharField(
        max_length=300,
        blank=True,
        help_text="The real numbers, e.g. 'handled ~30 tickets/day'. Only these may be quoted.",
    )
    skills = models.ManyToManyField(Skill, blank=True, related_name="+")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.text[:60]


class Project(UserOwnedModel, TimeStampedModel):
    name = models.CharField(max_length=150)
    role = models.CharField(max_length=120, blank=True)
    url = models.URLField(blank=True)
    description = models.TextField(max_length=2000, blank=True)
    technologies = models.ManyToManyField(Skill, blank=True, related_name="+")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    highlights = models.JSONField(default=list, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.name


class Education(UserOwnedModel, TimeStampedModel):
    institution = models.CharField(max_length=150)
    qualification = models.CharField(max_length=150, blank=True)
    field = models.CharField(max_length=150, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    grade = models.CharField(max_length=60, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name_plural = "education"

    def __str__(self) -> str:
        return self.institution


class Certification(UserOwnedModel, TimeStampedModel):
    class Status(models.TextChoices):
        EARNED = "earned", "Earned"
        IN_PROGRESS = "in_progress", "In progress"

    name = models.CharField(max_length=150)
    issuer = models.CharField(max_length=150, blank=True)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    credential_url = models.URLField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.EARNED)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.name


def cv_upload_path(instance: "CVImport", filename: str) -> str:
    """A random name, keeping only the validated extension. The user's file name never touches
    the file system."""
    return f"cv/{uuid.uuid4().hex}{PurePath(filename).suffix.lower()}"


class CVImport(UserOwnedModel, TimeStampedModel):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PARSING = "parsing", "Parsing"
        PARSED = "parsed", "Ready to review"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"

    file = models.FileField(upload_to=cv_upload_path, storage=private_storage)
    original_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UPLOADED)
    parsed_data = models.JSONField(null=True, blank=True)
    # A short, user-safe message; never CV content.
    error = models.CharField(max_length=300, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"CV import #{self.pk}"
