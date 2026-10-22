"""LLM bookkeeping: pricing, quotas, the system cap, the result cache and usage summaries.

The provider call itself lives in `client.py`. Quotas count calendar months in UTC. Counters are
read before each call and are not locked, so two simultaneous calls can overshoot a limit by one;
that is acceptable for a small invite-only user base.
"""

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.accounts.models import User
from apps.core.logging import log_event

from .exceptions import QuotaExceeded
from .models import LLMCache, LLMCall, UsageQuota

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)
_COST_PLACES = Decimal("0.000001")

# Quota groups -> the tasks that consume them. One kit is counted once, as its `generate_cv` call;
# the other kit documents are bounded by the USD cap. Manual extractions are user-triggered ones.
TASK_GROUPS: dict[str, frozenset[str]] = {
    "scores": frozenset({LLMCall.Task.SCORE_MATCH}),
    "kits": frozenset({LLMCall.Task.GENERATE_CV}),
    "manual": frozenset({LLMCall.Task.EXTRACT_JOB, LLMCall.Task.EXTRACT_PAGE}),
}
GROUP_LABELS = {
    "scores": "Job scoring",
    "kits": "Application kits",
    "manual": "Manual job extractions",
}
_GROUP_LIMIT_FIELDS = {
    "scores": "monthly_scores",
    "kits": "monthly_kits",
    "manual": "monthly_manual_extractions",
}
_GROUP_MESSAGES = {
    "scores": "You have used all of this month's job scoring allowance.",
    "kits": "You have used all of this month's application kits.",
    "manual": "You have used all of this month's manual job extractions.",
    "cost": "You have reached this month's AI usage limit.",
    "system_daily": "AI features are paused for today. Please try again tomorrow.",
}


@dataclass(frozen=True)
class GroupUsage:
    key: str
    label: str
    used: int
    limit: int

    @property
    def percent(self) -> int:
        return 100 if self.limit == 0 else min(100, round(self.used * 100 / self.limit))


@dataclass(frozen=True)
class UsageSummary:
    month_start: datetime.datetime
    groups: list[GroupUsage]
    cost_spent: Decimal
    cost_cap: Decimal

    @property
    def cost_percent(self) -> int:
        if self.cost_cap <= 0:
            return 100
        return min(100, round(self.cost_spent * 100 / self.cost_cap))


def month_start(now: datetime.datetime | None = None) -> datetime.datetime:
    now = now or timezone.now()
    now = now.astimezone(datetime.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start(now: datetime.datetime | None = None) -> datetime.datetime:
    now = (now or timezone.now()).astimezone(datetime.UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def group_for(task: str) -> str | None:
    for group, tasks in TASK_GROUPS.items():
        if task in tasks:
            return group
    return None


# --- pricing -------------------------------------------------------------------------------


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """USD cost from `LLM_PRICES_JSON`. An unpriced model costs 0 and is logged (spec §11.3)."""
    price = settings.LLM_PRICES.get(model)
    if not price:
        if settings.LLM_PROVIDER != "fake":
            log_event(
                logger,
                logging.WARNING,
                "llm_price_missing",
                "No price configured for model; cost recorded as 0",
                model=model,
            )
        return Decimal("0")
    per_in = Decimal(str(price.get("input_per_mtok", 0)))
    per_out = Decimal(str(price.get("output_per_mtok", 0)))
    cost = (per_in * input_tokens + per_out * output_tokens) / _MILLION
    return cost.quantize(_COST_PLACES)


# --- quotas --------------------------------------------------------------------------------


def _quota_for(user: User) -> UsageQuota:
    quota, _ = UsageQuota.objects.get_or_create(user=user)
    return quota


def _group_used(user: User, group: str, since: datetime.datetime) -> int:
    return (
        LLMCall.objects.for_user(user)
        .filter(task__in=TASK_GROUPS[group], success=True, cache_hit=False, created_at__gte=since)
        .count()
    )


def _spent(since: datetime.datetime, user: User | None) -> Decimal:
    queryset = LLMCall.objects.filter(created_at__gte=since)
    queryset = (
        queryset.filter(user=user) if user is not None else queryset.filter(user__isnull=True)
    )
    total: Decimal | None = queryset.aggregate(total=Sum("cost_usd"))["total"]
    return total or Decimal("0")


def _quota_error(scope: str, user_id: int | None) -> QuotaExceeded:
    log_event(
        logger,
        logging.WARNING,
        "quota_exceeded",
        "LLM quota exceeded",
        scope=scope,
        user_id_checked=user_id,
    )
    return QuotaExceeded(scope, _GROUP_MESSAGES[scope])


def check_quota(user: User | None, task: str) -> None:
    """Raise `QuotaExceeded` if this call is not allowed. Called before any provider request."""
    if user is None:
        cap = Decimal(str(settings.LLM_SYSTEM_DAILY_USD_CAP))
        if _spent(_day_start(), None) >= cap:
            raise _quota_error("system_daily", None)
        return

    start = month_start()
    quota = _quota_for(user)
    group = group_for(task)
    if group is not None:
        limit: int = getattr(quota, _GROUP_LIMIT_FIELDS[group])
        if _group_used(user, group, start) >= limit:
            raise _quota_error(group, user.pk)
    if _spent(start, user) >= quota.monthly_cost_usd_cap:
        raise _quota_error("cost", user.pk)


def usage_summary(user: User) -> UsageSummary:
    """This month's usage for one user, for Settings → Usage."""
    start = month_start()
    quota = _quota_for(user)
    groups = [
        GroupUsage(
            key=group,
            label=GROUP_LABELS[group],
            used=_group_used(user, group, start),
            limit=getattr(quota, _GROUP_LIMIT_FIELDS[group]),
        )
        for group in TASK_GROUPS
    ]
    return UsageSummary(
        month_start=start,
        groups=groups,
        cost_spent=_spent(start, user),
        cost_cap=quota.monthly_cost_usd_cap,
    )


# --- cache ---------------------------------------------------------------------------------


def get_cached(key: str, prompt_version: str) -> Any | None:
    entry = LLMCache.objects.filter(key=key, prompt_version=prompt_version).first()
    return entry.result if entry else None


def store_cached(key: str, prompt_version: str, result: Any) -> None:
    LLMCache.objects.update_or_create(
        key=key, prompt_version=prompt_version, defaults={"result": result}
    )


# --- staff summary -------------------------------------------------------------------------


def spend_summary() -> dict[str, Any]:
    """Spend and call counts for staff: today, this month, by task and by user (IDs only)."""
    start = month_start()
    today = _day_start()
    month_calls = LLMCall.objects.filter(created_at__gte=start)

    def totals(since: datetime.datetime) -> dict[str, Any]:
        agg = LLMCall.objects.filter(created_at__gte=since).aggregate(
            cost=Sum("cost_usd"),
            calls=Count("id"),
            failures=Count("id", filter=Q(success=False)),
            cache_hits=Count("id", filter=Q(cache_hit=True)),
        )
        return {**agg, "cost": agg["cost"] or Decimal("0")}

    return {
        "today": totals(today),
        "month": totals(start),
        "by_task": list(
            month_calls.values("task")
            .annotate(cost=Sum("cost_usd"), calls=Count("id"))
            .order_by("-cost", "task")
        ),
        "top_users": list(
            month_calls.filter(user__isnull=False)
            .values("user_id")
            .annotate(cost=Sum("cost_usd"), calls=Count("id"))
            .order_by("-cost")[:5]
        ),
    }
