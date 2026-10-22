import datetime
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone

from apps.llm.exceptions import QuotaExceeded
from apps.llm.models import LLMCall, UsageQuota
from apps.llm.services import (
    check_quota,
    compute_cost,
    get_cached,
    group_for,
    month_start,
    spend_summary,
    store_cached,
    usage_summary,
)
from tests.factories import UserFactory
from tests.logcapture import ListHandler

pytestmark = pytest.mark.django_db
UTC = datetime.UTC


def _call(user, task="score_match", **kwargs):  # type: ignore[no-untyped-def]
    defaults = {"provider": "fake", "model": "m", "success": True}
    return LLMCall.objects.create(user=user, task=task, **{**defaults, **kwargs})


# --- cost ------------------------------------------------------------------------------------


def test_cost_uses_the_price_table(settings) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_PRICES = {"m": {"input_per_mtok": 0.15, "output_per_mtok": 0.60}}
    assert compute_cost("m", 1_000_000, 1_000_000) == Decimal("0.750000")
    assert compute_cost("m", 2_000, 500) == Decimal("0.000600")


def test_cost_for_a_free_or_zero_token_call_is_zero(settings) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_PRICES = {"m": {"input_per_mtok": 1, "output_per_mtok": 1}}
    assert compute_cost("m", 0, 0) == Decimal("0")


def test_unpriced_model_costs_zero_and_warns_for_real_providers(
    settings, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_PRICES = {}
    settings.LLM_PROVIDER = "openai_compat"
    assert compute_cost("mystery", 1000, 1000) == Decimal("0")
    assert handler.events("llm_price_missing")


def test_unpriced_fake_model_is_silent(settings, handler: ListHandler) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_PRICES = {}
    assert compute_cost("fake-fast", 10, 10) == Decimal("0")
    assert not handler.events("llm_price_missing")


# --- months and groups -----------------------------------------------------------------------


def test_month_start_is_the_first_of_the_month_in_utc() -> None:
    assert month_start(datetime.datetime(2026, 3, 17, 15, 30, tzinfo=UTC)) == datetime.datetime(
        2026, 3, 1, tzinfo=UTC
    )
    # 01:00 on 1 Apr in Nairobi (UTC+3) is still 31 Mar in UTC.
    nairobi = datetime.timezone(datetime.timedelta(hours=3))
    assert month_start(datetime.datetime(2026, 4, 1, 1, 0, tzinfo=nairobi)).month == 3


@pytest.mark.parametrize(
    ("task", "group"),
    [
        ("score_match", "scores"),
        ("generate_cv", "kits"),
        ("extract_job", "manual"),
        ("extract_page", "manual"),
        ("parse_cv", None),
        ("generate_cover_letter", None),
    ],
)
def test_task_groups(task: str, group: str | None) -> None:
    assert group_for(task) == group


# --- per-user quota --------------------------------------------------------------------------


def test_quota_allows_calls_under_the_limit_and_blocks_at_it() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=2)
    _call(user)
    check_quota(user, "score_match")  # 1 of 2 used
    _call(user)
    with pytest.raises(QuotaExceeded) as info:
        check_quota(user, "score_match")
    assert info.value.scope == "scores" and "scoring" in info.value.message


def test_only_successful_non_cached_calls_this_month_count() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=1)
    _call(user, success=False)
    _call(user, cache_hit=True)
    _call(user, task="parse_cv")
    last_month = _call(user)
    LLMCall.objects.filter(pk=last_month.pk).update(
        created_at=month_start() - datetime.timedelta(days=1)
    )
    check_quota(user, "score_match")  # none of the above count


def test_quota_resets_at_the_start_of_the_next_month() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=1)
    with time_machine.travel(datetime.datetime(2026, 10, 31, 23, 59, tzinfo=UTC), tick=False):
        _call(user)
        with pytest.raises(QuotaExceeded):
            check_quota(user, "score_match")
    with time_machine.travel(datetime.datetime(2026, 11, 1, 0, 1, tzinfo=UTC), tick=False):
        check_quota(user, "score_match")


def test_groups_are_independent() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=1, monthly_kits=5)
    _call(user)
    check_quota(user, "generate_cv")
    with pytest.raises(QuotaExceeded):
        check_quota(user, "score_match")


def test_users_are_counted_separately() -> None:
    a, b = UserFactory(), UserFactory()
    UsageQuota.objects.create(user=a, monthly_scores=1)
    UsageQuota.objects.create(user=b, monthly_scores=1)
    _call(a)
    check_quota(b, "score_match")


def test_ungrouped_tasks_are_only_limited_by_cost() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=0, monthly_kits=0)
    check_quota(user, "parse_cv")


def test_monthly_cost_cap_blocks_every_task() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_cost_usd_cap=Decimal("1.00"))
    _call(user, task="parse_cv", cost_usd=Decimal("0.60"))
    check_quota(user, "parse_cv")
    _call(user, task="parse_cv", cost_usd=Decimal("0.40"))
    with pytest.raises(QuotaExceeded) as info:
        check_quota(user, "parse_cv")
    assert info.value.scope == "cost"


def test_failed_calls_still_count_towards_spend() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_cost_usd_cap=Decimal("0.10"))
    _call(user, task="parse_cv", success=False, cost_usd=Decimal("0.10"))
    with pytest.raises(QuotaExceeded):
        check_quota(user, "parse_cv")


def test_a_missing_quota_row_is_created_with_defaults(settings) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    check_quota(user, "score_match")
    assert UsageQuota.objects.get(user=user).monthly_scores == settings.QUOTA_DEFAULT_MONTHLY_SCORES


# --- system cap ------------------------------------------------------------------------------


def test_system_cap_counts_only_system_calls_today(settings) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_SYSTEM_DAILY_USD_CAP = "1.00"
    user = UserFactory()
    _call(user, cost_usd=Decimal("5"))  # a user's spend does not use the system cap
    check_quota(None, "extract_job")

    old = _call(None, task="extract_job", cost_usd=Decimal("5"))
    LLMCall.objects.filter(pk=old.pk).update(created_at=timezone.now() - datetime.timedelta(days=1))
    check_quota(None, "extract_job")  # yesterday's spend does not count

    _call(None, task="extract_job", cost_usd=Decimal("1.00"))
    with pytest.raises(QuotaExceeded) as info:
        check_quota(None, "extract_job")
    assert info.value.scope == "system_daily"


# --- summaries and cache ---------------------------------------------------------------------


def test_usage_summary_reports_groups_and_spend() -> None:
    user = UserFactory()
    UsageQuota.objects.create(
        user=user, monthly_scores=10, monthly_kits=4, monthly_manual_extractions=6,
        monthly_cost_usd_cap=Decimal("2.00"),
    )  # fmt: skip
    for _ in range(5):
        _call(user, cost_usd=Decimal("0.10"))
    _call(user, task="generate_cv", cost_usd=Decimal("0.30"))

    summary = usage_summary(user)
    by_key = {g.key: g for g in summary.groups}
    assert (by_key["scores"].used, by_key["scores"].limit, by_key["scores"].percent) == (5, 10, 50)
    assert (by_key["kits"].used, by_key["kits"].limit) == (1, 4)
    assert by_key["manual"].used == 0
    assert summary.cost_spent == Decimal("0.80") and summary.cost_cap == Decimal("2.00")
    assert summary.cost_percent == 40


def test_percent_is_capped_and_handles_zero_limits() -> None:
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_scores=0, monthly_cost_usd_cap=Decimal("0"))
    summary = usage_summary(user)
    assert next(g for g in summary.groups if g.key == "scores").percent == 100
    assert summary.cost_percent == 100


def test_cache_round_trip_and_version_separation() -> None:
    assert get_cached("k", "1") is None
    store_cached("k", "1", {"a": 1})
    store_cached("k", "1", {"a": 2})  # overwrite, not duplicate
    store_cached("k", "2", {"a": 3})
    assert get_cached("k", "1") == {"a": 2}
    assert get_cached("k", "2") == {"a": 3}


def test_spend_summary_totals_and_breakdowns() -> None:
    a, b = UserFactory(), UserFactory()
    _call(a, cost_usd=Decimal("0.50"))
    _call(a, task="generate_cv", cost_usd=Decimal("0.25"), success=False)
    _call(b, cost_usd=Decimal("0.05"), cache_hit=True)
    _call(None, task="extract_job", cost_usd=Decimal("0.10"))

    summary = spend_summary()
    assert summary["today"]["cost"] == Decimal("0.90")
    assert summary["month"]["calls"] == 4 and summary["month"]["failures"] == 1
    assert summary["month"]["cache_hits"] == 1
    assert summary["by_task"][0]["task"] == "score_match"
    assert [row["user_id"] for row in summary["top_users"]] == [a.pk, b.pk]
