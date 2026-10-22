"""LLM models. Phase 1 holds only `UsageQuota`; `LLMCall` and enforcement arrive in Phase 2."""

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
