"""Job sources and their run history (spec §5.4)."""

from django.db import models

from apps.core.models import TimeStampedModel


class Source(TimeStampedModel):
    class Kind(models.TextChoices):
        JSONLD = "jsonld", "JSON-LD pages"
        ATS = "ats", "Applicant tracking system board"
        JSON_API = "json_api", "JSON API"
        RSS = "rss", "RSS / Atom feed"
        LLM_EXTRACT = "llm_extract", "LLM page extraction"
        CUSTOM = "custom", "Custom plugin"

    class Health(models.TextChoices):
        OK = "ok", "OK"
        DEGRADED = "degraded", "Degraded"
        FAILING = "failing", "Failing"

    name = models.SlugField(max_length=60, unique=True)
    display_name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    config = models.JSONField(default=dict, blank=True, help_text="Keys depend on the kind.")
    interval_minutes = models.PositiveIntegerField(default=20)
    enabled = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    health = models.CharField(max_length=8, choices=Health.choices, default=Health.OK)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.display_name or self.name


class SourceRun(models.Model):
    class Trigger(models.TextChoices):
        SCHEDULE = "schedule", "Schedule"
        MANUAL = "manual", "Manual"
        ADMIN = "admin", "Admin"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="runs")
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.SCHEDULE)
    ids_seen = models.PositiveIntegerField(default=0)
    new_jobs = models.PositiveIntegerField(default=0)
    updated_jobs = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    success = models.BooleanField(default=False)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [models.Index(fields=["source", "-started_at"])]

    def __str__(self) -> str:
        return f"Run #{self.pk} of {self.source_id}"
