"""LLM models: the call log, the result cache and per-user quotas (spec §5.11)."""

from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel, UserOwnedQuerySet


# Module-level callables (not lambdas) so migrations can serialise the defaults.
def _default_monthly_scores() -> int:
    return int(settings.QUOTA_DEFAULT_MONTHLY_SCORES)


def _default_monthly_kits() -> int:
    return int(settings.QUOTA_DEFAULT_MONTHLY_KITS)


def _default_monthly_manual() -> int:
    return int(settings.QUOTA_DEFAULT_MONTHLY_MANUAL)


def _default_monthly_cost_cap() -> Decimal:
    return Decimal(str(settings.QUOTA_DEFAULT_MONTHLY_USD))


class UsageQuota(TimeStampedModel):
    """Per-user monthly LLM limits. Defaults come from settings; edited per user in the admin."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="usage_quota"
    )
    monthly_scores = models.PositiveIntegerField(default=_default_monthly_scores)
    monthly_kits = models.PositiveIntegerField(default=_default_monthly_kits)
    monthly_manual_extractions = models.PositiveIntegerField(default=_default_monthly_manual)
    monthly_cost_usd_cap = models.DecimalField(
        max_digits=8, decimal_places=2, default=_default_monthly_cost_cap
    )

    objects = UserOwnedQuerySet.as_manager()

    def __str__(self) -> str:
        return f"Quota of user {self.user_id}"


class LLMCall(TimeStampedModel):
    """One logical LLM call (a repair retry is folded into the same row).

    Prompt and response bodies are stored only when `LLM_LOG_BODIES=True` (dev debugging);
    otherwise those fields stay empty. Rows are kept 12 months (retention job: Phase 11).
    """

    class Task(models.TextChoices):
        EXTRACT_JOB = "extract_job"
        SCORE_MATCH = "score_match"
        PARSE_CV = "parse_cv"
        GENERATE_CV = "generate_cv"
        GENERATE_COVER_LETTER = "generate_cover_letter"
        GENERATE_INTERVIEW = "generate_interview"
        GENERATE_SCREENING = "generate_screening"
        EXTRACT_EMAIL_JOBS = "extract_email_jobs"
        EXTRACT_PAGE = "extract_page"
        SMOKE = "smoke"  # only used by the llm_smoke management command

    # Null for system work such as shared job extraction. Deleting a user removes their calls.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
    )
    task = models.CharField(max_length=30, choices=Task.choices)
    provider = models.CharField(max_length=30)
    model = models.CharField(max_length=100)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal("0"))
    latency_ms = models.PositiveIntegerField(default=0)
    success = models.BooleanField(default=True)
    # A short machine-readable summary (exception type and field names); never content.
    error = models.CharField(max_length=300, blank=True)
    cache_hit = models.BooleanField(default=False)
    prompt_version = models.CharField(max_length=20, blank=True)
    prompt_body = models.TextField(blank=True)
    response_body = models.TextField(blank=True)

    objects = UserOwnedQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["task", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"LLM call #{self.pk} ({self.task})"


class LLMCache(TimeStampedModel):
    """Stored results for repeatable calls (job extraction, profile summaries).

    The caller owns the key, so per-user results must include the user in it.
    """

    key = models.CharField(max_length=200)
    prompt_version = models.CharField(max_length=20)
    result = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "prompt_version"], name="llmcache_key_version"),
        ]

    def __str__(self) -> str:
        return f"Cache entry #{self.pk}"
