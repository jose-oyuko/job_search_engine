"""Matching services: contexts, matches, rematching, scoring, quotas, statuses, the feed, search."""

import datetime
import json
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone

from apps.accounts.models import User, UserSettings
from apps.jobs.models import Job
from apps.jobs.tasks import extract_job_task
from apps.llm.models import LLMCall, UsageQuota
from apps.llm.providers.fake import FakeProvider
from apps.matching import services
from apps.matching.exceptions import NotOwner, SearchTooSoon
from apps.matching.models import Match
from apps.profiles import services as profile_services
from apps.profiles.models import Track
from apps.sources.models import SourceRun
from tests.factories import (
    ExperienceFactory,
    JobFactory,
    MatchFactory,
    SkillFactory,
    SourceFactory,
    TrackFactory,
    UserFactory,
)
from tests.matching_helpers import default_track, make_job, make_user

pytestmark = pytest.mark.django_db
UTC = datetime.UTC


def scored(**overrides: object) -> str:
    base = {"score": 72, "verdict": "good", "strengths": ["a"], "gaps": ["b"], "summary": "Fits."}
    return json.dumps({**base, **overrides})


# --- who can be matched -----------------------------------------------------------------------------------


def test_a_user_with_a_track_and_profile_content_can_be_matched() -> None:
    user = make_user(country="UG")
    ctx = services.build_context(user)
    assert ctx is not None and ctx.country == "UG" and [t.name for t in ctx.tracks] == ["Support"]
    assert sorted(ctx.skill_names) == ["Excel", "Helpdesk"] and ctx.profile_version >= 1


def test_no_track_means_no_context() -> None:
    assert services.build_context(make_user(track=False)) is None


def test_an_empty_profile_means_no_context() -> None:
    user = make_user(skills=(), headline="")
    assert services.build_context(user) is None
    ExperienceFactory(user=user)  # any experience is enough
    assert services.build_context(user) is not None


def test_a_summary_or_skills_alone_are_enough() -> None:
    user = make_user(skills=(), headline="")
    profile = profile_services.get_profile(user)
    profile.summary = "I help people."
    profile.save()
    assert services.build_context(user) is not None


def test_inactive_users_are_not_matched() -> None:
    user = make_user()
    User.objects.filter(pk=user.pk).update(is_active=False)
    user.refresh_from_db()
    assert services.build_context(user) is None


def test_country_defaults_to_kenya_without_settings() -> None:
    user = make_user()
    UserSettings.objects.filter(user=user).delete()
    ctx = services.build_context(user)
    assert ctx is not None and ctx.country == "KE"


def test_tracks_are_listed_default_first() -> None:
    user = make_user()
    TrackFactory(user=user, name="Aardvark")
    ctx = services.build_context(user)
    assert ctx is not None and [t.name for t in ctx.tracks] == ["Support", "Aardvark"]


# --- picking a track ----------------------------------------------------------------------------------------


def test_the_track_with_the_best_keyword_overlap_wins() -> None:
    user = make_user(target_titles=["engineer"])
    TrackFactory(
        user=user,
        name="Support",
        target_titles=["customer support", "support"],
        work_types=["remote"],
    )
    ctx = services.build_context(user)
    job = make_job(title="Customer Support Engineer")
    track, result = services.pick_track(job, ctx)  # type: ignore[arg-type]
    assert track.name == "Support" and result.passed


def test_a_passing_track_beats_a_better_matching_failing_one() -> None:
    user = make_user(
        target_titles=["support"], work_types=["onsite"]
    )  # default track: fails (remote job)
    TrackFactory(user=user, name="Remote", target_titles=["agent"], work_types=["remote"])
    ctx = services.build_context(user)
    track, result = services.pick_track(make_job(title="Support Agent"), ctx)  # type: ignore[arg-type]
    assert track.name == "Remote" and result.passed


def test_ties_go_to_the_default_track() -> None:
    user = make_user()
    TrackFactory(user=user, name="Zed", target_titles=["support"], work_types=["remote"])
    ctx = services.build_context(user)
    track, _ = services.pick_track(make_job(), ctx)  # type: ignore[arg-type]
    assert track.is_default


# --- creating and updating matches ----------------------------------------------------------------------------


def test_upsert_creates_a_match_with_reasons_and_the_track() -> None:
    user = make_user()
    ctx = services.build_context(user)
    match, created = services.upsert_match(ctx, make_job())  # type: ignore[arg-type]
    assert created and match is not None and match.rule_pass and match.status == "new"
    assert match.track == default_track(user) and "eligible: global" in match.rule_reasons
    assert match.score is None


def test_upsert_twice_does_not_duplicate_or_touch_the_user_state() -> None:
    user = make_user()
    ctx = services.build_context(user)
    job = make_job()
    first, _ = services.upsert_match(ctx, job)  # type: ignore[arg-type]
    Match.objects.filter(pk=first.pk).update(status="saved", score=80, verdict="strong")  # type: ignore[union-attr]
    second, created = services.upsert_match(ctx, job)  # type: ignore[arg-type]
    assert not created and second.pk == first.pk  # type: ignore[union-attr]
    second.refresh_from_db()  # type: ignore[union-attr]
    assert (second.status, second.score) == ("saved", 80)  # type: ignore[union-attr]
    assert Match.objects.count() == 1


def test_a_failing_job_gets_no_match() -> None:
    user = make_user()
    ctx = services.build_context(user)
    assert services.upsert_match(ctx, make_job(work_type="onsite")) == (None, False)  # type: ignore[arg-type]
    assert Match.objects.count() == 0


def test_a_match_that_starts_failing_is_removed_unless_the_user_saved_it() -> None:
    user = make_user()
    ctx = services.build_context(user)
    plain = make_job()
    keeper = make_job(title="Support Specialist")
    applied = make_job(title="Support Analyst")
    for job in (plain, keeper, applied):
        services.upsert_match(ctx, job)  # type: ignore[arg-type]
    Match.objects.filter(job=keeper).update(status="saved")
    Match.objects.filter(job=applied).update(status="applied")
    Track.objects.filter(user=user).update(exclude_keywords=["support"])  # now everything fails
    ctx = services.build_context(user)

    for job in (plain, keeper, applied):
        services.upsert_match(ctx, job)  # type: ignore[arg-type]

    assert not Match.objects.filter(job=plain).exists()
    for job in (keeper, applied):
        match = Match.objects.get(job=job)
        assert match.rule_pass is False and any("excluded keyword" in r for r in match.rule_reasons)


def test_forced_matches_are_kept_even_when_the_rules_fail() -> None:
    user = make_user()
    ctx = services.build_context(user)
    match, created = services.upsert_match(ctx, make_job(work_type="onsite"), force=True)  # type: ignore[arg-type]
    assert created and match is not None and match.rule_pass is False
    assert any("not wanted" in r for r in match.rule_reasons)


def test_private_jobs_of_another_user_are_not_matched_by_the_batch() -> None:
    owner, other = make_user(), make_user()
    job = make_job(visibility="private", owner=owner, source=None, origin="manual_text")
    assert services.match_new_job(job.pk) == []
    assert Match.objects.count() == 0 and other


# --- matching a new job against every user ---------------------------------------------------------------------


def test_match_new_job_creates_matches_for_the_users_whose_rules_pass() -> None:
    keen = make_user()
    onsite_only = make_user(work_types=["onsite"])
    make_user(track=False)  # no track: never matched
    job = make_job()
    ids = services.match_new_job(job.pk)
    assert [Match.objects.get(pk=i).user for i in ids] == [keen]
    assert not Match.objects.filter(user=onsite_only).exists()


def test_match_new_job_is_idempotent_and_never_rescores_existing_matches() -> None:
    user = make_user()
    job = make_job()
    first = services.match_new_job(job.pk)
    Match.objects.filter(user=user).update(score=90, verdict="strong")
    assert services.match_new_job(job.pk) == []  # nothing new to create or score
    assert len(first) == 1 and Match.objects.get().score == 90


@pytest.mark.parametrize(
    "fields",
    [{"status": "expired"}, {"visibility": "private"}],
)
def test_match_new_job_skips_expired_and_private_jobs(fields: dict[str, str]) -> None:
    make_user()
    assert services.match_new_job(make_job(**fields).pk) == []


def test_match_new_job_skips_duplicates_and_missing_jobs() -> None:
    make_user()
    original = make_job()
    duplicate = make_job(duplicate_of=original)
    assert services.match_new_job(duplicate.pk) == [] and services.match_new_job(999_999) == []


def test_new_jobs_flow_from_extraction_to_a_scored_match(
    fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    job = make_job()
    with django_capture_on_commit_callbacks(execute=True):
        extract_job_task(job.pk)  # Huey runs it immediately in tests
    match = Match.objects.get(user=user)
    assert match.score == 72 and match.verdict == "good" and match.scoring_queued_at is None
    assert LLMCall.objects.filter(task="extract_job", user__isnull=True).count() == 1
    assert LLMCall.objects.filter(task="score_match", user=user).count() == 1


def test_matching_happens_even_if_extraction_fails(
    fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    job = make_job()
    fake_llm.script.extend(["junk", "junk"])  # extraction fails
    with django_capture_on_commit_callbacks(execute=True):
        extract_job_task(job.pk)
    assert Match.objects.filter(user=user, job=job).exists()


def test_manual_jobs_are_matched_immediately_for_their_user(
    django_capture_on_commit_callbacks,
) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    job = make_job(
        work_type="onsite", visibility="private", owner=user, source=None
    )  # fails the rules
    with django_capture_on_commit_callbacks(execute=True):
        match = services.match_job_for_user(user, job)
    assert match is not None and match.rule_pass is False
    match.refresh_from_db()
    assert match.score == 72  # scored in the background anyway


def test_manual_matching_does_nothing_for_users_without_a_target() -> None:
    user = make_user(track=False)
    assert services.match_job_for_user(user, make_job()) is None


# --- rematching ----------------------------------------------------------------------------------------------------


def test_rematch_covers_the_last_14_days_only() -> None:
    user = make_user()
    recent = make_job()
    old = make_job(first_seen_at=timezone.now() - datetime.timedelta(days=15))
    result = services.rematch_user(user.pk)
    assert result.created == 1
    assert Match.objects.filter(job=recent).exists() and not Match.objects.filter(job=old).exists()


def test_rematch_includes_the_users_own_private_jobs_but_not_others() -> None:
    user, other = make_user(), make_user()
    mine = make_job(visibility="private", owner=user, source=None, title="Support Agent")
    theirs = make_job(visibility="private", owner=other, source=None, title="Support Agent")
    services.rematch_user(user.pk)
    assert Match.objects.filter(user=user, job=mine).exists()
    assert not Match.objects.filter(job=theirs, user=user).exists()


def test_changing_track_keywords_and_rematching_changes_the_feed() -> None:
    user = make_user(target_titles=["support"])
    support = make_job(title="Support Agent")
    cook = make_job(title="Cook Assistant", description_text="Kitchen work")
    services.rematch_user(user.pk)
    assert list(Match.objects.for_user(user).values_list("job", flat=True)) == [support.pk]

    Track.objects.filter(user=user).update(target_titles=["cook"], include_keywords=[])
    result = services.rematch_user(user.pk)

    assert (result.created, result.removed) == (1, 1)
    assert list(Match.objects.for_user(user).values_list("job", flat=True)) == [cook.pk]


def test_rematch_without_a_track_clears_unsaved_matches() -> None:
    user = make_user()
    job = make_job()
    services.rematch_user(user.pk)
    saved = MatchFactory(user=user, job=make_job(title="Other"), status="saved")
    Track.objects.filter(user=user).delete()
    result = services.rematch_user(user.pk)
    assert result.removed == 1 and not Match.objects.filter(job=job).exists()
    assert Match.objects.filter(pk=saved.pk).exists()


def test_rematch_ignores_unknown_or_inactive_users() -> None:
    assert services.rematch_user(999_999) == services.RematchResult(0, 0, 0)


def test_a_new_users_first_rematch_scores_a_few_matches(fake_llm: FakeProvider) -> None:
    user = make_user()
    for i in range(14):
        make_job(title=f"Support Agent {i}", external_id=f"j{i}")
    services.rematch_user(user.pk)
    scored_count = Match.objects.for_user(user).filter(score__isnull=False).count()
    assert scored_count == services.FIRST_PASS_SCORE_LIMIT
    assert len(fake_llm.calls) == services.FIRST_PASS_SCORE_LIMIT


def test_later_rematches_do_not_score_automatically(fake_llm: FakeProvider) -> None:
    user = make_user()
    old = make_job(title="Support Old")
    services.rematch_user(user.pk)
    Match.objects.filter(job=old).update(score=50, verdict="weak", scored_profile_version=1)
    fake_llm.calls.clear()
    make_job(title="Support New", external_id="new")
    services.rematch_user(user.pk)
    assert fake_llm.calls == []  # new matches wait to be scored lazily (cost control)


def test_profile_changes_schedule_a_debounced_rematch(
    django_capture_on_commit_callbacks, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from apps.matching import tasks

    scheduled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        tasks.rematch_user_task, "schedule", lambda args, delay: scheduled.append((args[0], delay))
    )
    user = make_user()
    with django_capture_on_commit_callbacks(execute=True):
        profile = profile_services.get_profile(user)
        for headline in ("One", "Two", "Three"):
            profile.headline = headline
            profile_services.save_profile(user, profile)
    assert scheduled == [(user.pk, profile_services.REMATCH_DELAY_SECONDS)]  # three edits, one run


def test_a_track_change_schedules_a_rematch_that_then_finds_matches(
    django_capture_on_commit_callbacks, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from apps.matching import tasks

    scheduled: list[int] = []
    monkeypatch.setattr(
        tasks.rematch_user_task, "schedule", lambda args, delay: scheduled.append(args[0])
    )
    user = make_user()
    job = make_job()
    with django_capture_on_commit_callbacks(execute=True):
        profile_services.save_track(user, Track.objects.for_user(user).get(), make_default=True)
    assert scheduled == [user.pk]
    services.rematch_user(scheduled[0])  # what the worker does 30 seconds later
    assert Match.objects.filter(user=user, job=job).exists()


# --- scoring -------------------------------------------------------------------------------------------------------------


@pytest.fixture
def one_match() -> Match:
    user = make_user()
    ctx = services.build_context(user)
    match, _ = services.upsert_match(ctx, make_job())  # type: ignore[arg-type]
    assert match is not None
    return match


def test_scoring_stores_the_result(one_match: Match, fake_llm: FakeProvider) -> None:
    fake_llm.script.append(
        scored(
            score=84,
            verdict="strong",
            strengths=["Helpdesk match"],
            gaps=["No Zendesk"],
            summary="Strong fit.",
        )
    )
    assert services.score_match(one_match.pk) == services.SCORED
    one_match.refresh_from_db()
    assert (one_match.score, one_match.verdict, one_match.summary) == (84, "strong", "Strong fit.")
    assert one_match.strengths == ["Helpdesk match"] and one_match.gaps == ["No Zendesk"]
    assert one_match.scored_profile_version == profile_services.get_profile(one_match.user).version
    assert one_match.scored_at and one_match.llm_model
    call = LLMCall.objects.get(task="score_match")
    assert call.user == one_match.user and call.success


@pytest.mark.parametrize(
    ("answer", "score", "verdict"),
    [
        ({"score": 85, "verdict": "poor"}, 85, "strong"),
        ({"score": 65, "verdict": "strong"}, 65, "good"),
        ({"score": 45, "verdict": "good"}, 45, "weak"),
        ({"score": 10, "verdict": "good"}, 10, "poor"),
        ({"score": 250, "verdict": "strong"}, 100, "strong"),
        ({"score": -5, "verdict": "poor"}, 0, "poor"),
        ({"score": 80, "verdict": "strong"}, 80, "strong"),
        ({"score": 60, "verdict": "good"}, 60, "good"),
        ({"score": 40, "verdict": "weak"}, 40, "weak"),
    ],
)
def test_scores_are_clamped_and_verdicts_follow_the_score(
    one_match: Match, fake_llm: FakeProvider, answer: dict[str, object], score: int, verdict: str
) -> None:
    fake_llm.script.append(scored(**answer))
    services.score_match(one_match.pk)
    one_match.refresh_from_db()
    assert (one_match.score, one_match.verdict) == (score, verdict)


def test_lists_and_summary_are_trimmed(one_match: Match, fake_llm: FakeProvider) -> None:
    long = " ".join(f"w{i}" for i in range(30))
    fake_llm.script.append(
        scored(strengths=[long, "", "  ", "b", "c", "d", "e"], gaps=[long] * 6, summary=long)
    )
    services.score_match(one_match.pk)
    one_match.refresh_from_db()
    assert len(one_match.strengths) == 4 and all(len(s.split()) <= 16 for s in one_match.strengths)
    assert one_match.strengths[0].endswith("…") and "" not in one_match.strengths
    assert len(one_match.gaps) == 4 and len(one_match.summary.split()) <= 26


def test_an_up_to_date_score_is_left_alone(one_match: Match, fake_llm: FakeProvider) -> None:
    services.score_match(one_match.pk)
    assert services.score_match(one_match.pk) == services.SKIPPED
    assert len(fake_llm.calls) == 1


def test_a_stale_score_is_recomputed_after_the_profile_changes(
    one_match: Match, fake_llm: FakeProvider
) -> None:
    services.score_match(one_match.pk)
    user = one_match.user
    SkillFactory(user=user, name="Zendesk")
    profile = profile_services.get_profile(user)
    profile_services.save_profile(user, profile)  # bumps the version
    one_match.refresh_from_db()
    assert one_match.is_stale(profile_services.get_profile(user).version)
    assert services.score_match(one_match.pk) == services.SCORED and len(fake_llm.calls) == 2


def test_the_same_inputs_are_answered_from_the_cache(
    one_match: Match, fake_llm: FakeProvider
) -> None:
    services.score_match(one_match.pk)
    Match.objects.filter(pk=one_match.pk).update(score=None, scored_profile_version=None)
    services.score_match(one_match.pk)
    assert len(fake_llm.calls) == 1 and LLMCall.objects.filter(cache_hit=True).count() == 1


def test_the_prompt_carries_the_profile_the_track_and_the_job_as_untrusted_data(
    one_match: Match, fake_llm: FakeProvider
) -> None:
    services.score_match(one_match.pk)
    sent = fake_llm.calls[0]
    content = sent["messages"][0]["content"]
    assert (
        "<candidate>" in content
        and "Headline: Support specialist" in content
        and "Skills:" in content
    )
    assert "Target track: Support" in content and "Wants: remote" in content
    assert (
        "<job_posting>" in content
        and "Title: Customer Support Agent" in content
        and "Answer customers." in content
    )
    assert "NEVER credit a skill" in sent["system"] and "untrusted" in sent["system"]


def test_extracted_facts_replace_the_raw_description_when_available(
    one_match: Match, fake_llm: FakeProvider
) -> None:
    Job.objects.filter(pk=one_match.job_id).update(
        extracted={
            "summary": "Answer tickets.",
            "required_skills": ["Zendesk", "Excel"],
            "seniority": "junior",
        },
        description_text="RAW DESCRIPTION TEXT",
    )
    services.score_match(one_match.pk)
    content = fake_llm.calls[0]["messages"][0]["content"]
    assert "Required skills: Zendesk; Excel" in content and "Seniority: junior" in content
    assert "RAW DESCRIPTION TEXT" not in content


def test_pay_requirements_are_part_of_the_prompt(fake_llm: FakeProvider) -> None:
    user = make_user(min_pay_amount=Decimal("15"), min_pay_currency="USD", min_pay_period="hour")
    ctx = services.build_context(user)
    match, _ = services.upsert_match(ctx, make_job(pay_text="$20/hr"))  # type: ignore[arg-type]
    services.score_match(match.pk)  # type: ignore[union-attr]
    content = fake_llm.calls[0]["messages"][0]["content"]
    assert "Minimum pay: 15" in content and "Pay: $20/hr" in content


def test_scoring_failures_leave_the_match_unscored(
    one_match: Match, fake_llm: FakeProvider, handler
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["junk", "junk"])
    assert services.score_match(one_match.pk) == services.FAILED
    one_match.refresh_from_db()
    assert one_match.score is None and handler.events("score_failed")


def test_unknown_matches_are_skipped() -> None:
    assert services.score_match(999_999) == services.SKIPPED


def test_logs_do_not_contain_job_or_profile_text(
    one_match: Match, fake_llm: FakeProvider, handler
) -> None:  # type: ignore[no-untyped-def]
    services.score_match(one_match.pk)
    output = "".join(handler.lines)
    assert (
        "Customer Support Agent" not in output
        and "Helpdesk" not in output
        and "Answer customers" not in output
    )
    assert handler.events("match_scored")


# --- quota --------------------------------------------------------------------------------------------------------------


def test_an_exhausted_allowance_blocks_scoring_without_calling_the_model(
    one_match: Match, fake_llm: FakeProvider
) -> None:
    UsageQuota.objects.create(user=one_match.user, monthly_scores=0)
    assert services.scores_exhausted(one_match.user)
    assert services.score_match(one_match.pk) == services.BLOCKED
    assert fake_llm.calls == [] and LLMCall.objects.count() == 0
    one_match.refresh_from_db()
    assert one_match.score is None


def test_the_allowance_runs_out_part_way_through_a_batch(fake_llm: FakeProvider) -> None:
    user = make_user()
    UsageQuota.objects.create(user=user, monthly_scores=2)
    ctx = services.build_context(user)
    for i in range(4):
        services.upsert_match(ctx, make_job(title=f"Support Agent {i}", external_id=f"j{i}"))  # type: ignore[arg-type]
    summary = services.score_pending_for_user(user, days=7)
    assert summary == services.ScoringSummary(scored=2, blocked=True)
    assert len(fake_llm.calls) == 2 and Match.objects.filter(score__isnull=False).count() == 2


def test_the_spend_cap_also_blocks_scoring(one_match: Match, fake_llm: FakeProvider) -> None:
    UsageQuota.objects.create(user=one_match.user, monthly_cost_usd_cap=Decimal("0"))
    assert services.score_match(one_match.pk) == services.BLOCKED


# --- queueing and lazy refresh ----------------------------------------------------------------------------------------------


def test_queue_scoring_marks_the_matches_and_runs_after_commit(
    one_match: Match, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        services.queue_scoring([one_match.pk])
        one_match.refresh_from_db()
        assert one_match.scoring_queued_at is not None and one_match.score is None  # not yet run
    one_match.refresh_from_db()
    assert one_match.score == 72 and one_match.scoring_queued_at is None
    services.queue_scoring([])  # harmless


def test_viewing_an_unscored_match_queues_scoring_once(
    one_match: Match, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    user, version = one_match.user, profile_services.get_profile(one_match.user).version
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assert services.ensure_scoring_queued(user, one_match, version) is True
    assert len(callbacks) == 1
    one_match.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assert services.ensure_scoring_queued(user, one_match, version) is True  # still pending
    assert callbacks == []  # not queued a second time


def test_a_fresh_score_needs_no_refresh(one_match: Match) -> None:
    services.score_match(one_match.pk)
    one_match.refresh_from_db()
    version = profile_services.get_profile(one_match.user).version
    assert services.ensure_scoring_queued(one_match.user, one_match, version) is False


def test_no_lazy_scoring_when_the_allowance_is_used_up(
    one_match: Match, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    UsageQuota.objects.create(user=one_match.user, monthly_scores=0)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assert services.ensure_scoring_queued(one_match.user, one_match, 1) is False
    assert callbacks == []


def test_an_old_queue_marker_is_retried(
    one_match: Match, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    Match.objects.filter(pk=one_match.pk).update(
        scoring_queued_at=timezone.now() - datetime.timedelta(minutes=30)
    )
    one_match.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assert services.ensure_scoring_queued(one_match.user, one_match, 1) is True
    assert len(callbacks) == 1


def test_rescore_clears_the_score_and_queues_a_new_one(
    one_match: Match, fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    services.score_match(one_match.pk)
    one_match.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        services.request_rescore(one_match.user, one_match)
    assert len(fake_llm.calls) == 1  # the identical input came from the cache, at no charge
    one_match.refresh_from_db()
    assert one_match.score == 72


def test_only_the_owner_can_rescore_or_refresh(one_match: Match) -> None:
    stranger = make_user()
    with pytest.raises(NotOwner):
        services.request_rescore(stranger, one_match)
    with pytest.raises(NotOwner):
        services.ensure_scoring_queued(stranger, one_match, 1)


def test_pending_scores_selection(fake_llm: FakeProvider) -> None:
    user = make_user()
    ctx = services.build_context(user)
    fresh = services.upsert_match(ctx, make_job(title="Support A", external_id="a"))[0]  # type: ignore[arg-type]
    old = services.upsert_match(
        ctx,
        make_job(
            title="Support B",
            external_id="b",
            first_seen_at=timezone.now() - datetime.timedelta(days=9),
        ),
    )[0]  # type: ignore[arg-type]
    dismissed = services.upsert_match(ctx, make_job(title="Support C", external_id="c"))[0]  # type: ignore[arg-type]
    failing = MatchFactory(
        user=user, job=make_job(title="Support D", external_id="d"), rule_pass=False
    )
    Match.objects.filter(pk=dismissed.pk).update(status="dismissed")  # type: ignore[union-attr]
    pending = list(services.pending_scores(user, days=7))
    assert pending == [fresh] and old not in pending and failing not in pending


# --- statuses -------------------------------------------------------------------------------------------------------------------


def test_status_changes_and_dismiss_reasons(one_match: Match) -> None:
    user = one_match.user
    services.set_status(user, one_match, "saved")
    assert Match.objects.get(pk=one_match.pk).status == "saved"
    services.set_status(user, one_match, "dismissed", reason="pay_too_low")
    got = Match.objects.get(pk=one_match.pk)
    assert (got.status, got.dismiss_reason) == ("dismissed", "pay_too_low")
    services.set_status(user, one_match, "seen")
    assert Match.objects.get(pk=one_match.pk).dismiss_reason == ""  # cleared when restored


def test_status_changes_are_validated_and_owner_checked(one_match: Match) -> None:
    with pytest.raises(ValueError):
        services.set_status(one_match.user, one_match, "teleported")
    with pytest.raises(NotOwner):
        services.set_status(make_user(), one_match, "saved")


def test_mark_seen_only_moves_new_matches(one_match: Match) -> None:
    services.mark_seen(one_match)
    assert Match.objects.get(pk=one_match.pk).status == "seen"
    Match.objects.filter(pk=one_match.pk).update(status="saved")
    services.mark_seen(one_match)
    assert Match.objects.get(pk=one_match.pk).status == "saved"


# --- the feed query ---------------------------------------------------------------------------------------------------------


def titles(user: User, **filters: object) -> list[str]:
    return [m.job.title for m in services.feed_queryset(user, services.FeedFilters(**filters))]  # type: ignore[arg-type]


def test_default_feed_ranks_by_score_then_recency_with_unscored_last() -> None:
    user = UserFactory()
    now = timezone.now()
    low = MatchFactory(
        user=user, job=make_job(title="Low", external_id="1", first_seen_at=now), score=40
    )
    high = MatchFactory(
        user=user,
        job=make_job(title="High", external_id="2", first_seen_at=now - datetime.timedelta(days=3)),
        score=90,
    )
    new_unscored = MatchFactory(
        user=user, job=make_job(title="Unscored new", external_id="3", first_seen_at=now)
    )
    old_unscored = MatchFactory(
        user=user,
        job=make_job(
            title="Unscored old", external_id="4", first_seen_at=now - datetime.timedelta(days=5)
        ),
    )
    tie_a = MatchFactory(
        user=user,
        job=make_job(
            title="Tie older", external_id="5", first_seen_at=now - datetime.timedelta(hours=5)
        ),
        score=40,
    )
    assert titles(user) == ["High", "Low", "Tie older", "Unscored new", "Unscored old"]
    assert {low, high, new_unscored, old_unscored, tie_a}


def test_default_feed_hides_failed_rules_dismissed_applied_and_duplicates() -> None:
    user = UserFactory()
    MatchFactory(user=user, job=make_job(title="Shown", external_id="1"))
    MatchFactory(user=user, job=make_job(title="Failed rules", external_id="2"), rule_pass=False)
    MatchFactory(
        user=user,
        job=make_job(title="Failed but saved", external_id="3"),
        rule_pass=False,
        status="saved",
    )
    MatchFactory(user=user, job=make_job(title="Dismissed", external_id="4"), status="dismissed")
    MatchFactory(user=user, job=make_job(title="Applied", external_id="5"), status="applied")
    MatchFactory(
        user=user,
        job=make_job(title="Duplicate", external_id="6", duplicate_of=make_job(external_id="7")),
    )
    assert sorted(titles(user)) == ["Failed but saved", "Shown"]


def test_status_filter_shows_the_hidden_ones() -> None:
    user = UserFactory()
    MatchFactory(user=user, job=make_job(title="Dismissed", external_id="1"), status="dismissed")
    MatchFactory(user=user, job=make_job(title="Applied", external_id="2"), status="applied")
    MatchFactory(user=user, job=make_job(title="Saved", external_id="3"), status="saved")
    assert titles(user, status="dismissed") == ["Dismissed"] and titles(user, status="applied") == [
        "Applied"
    ]
    assert titles(user, status="saved") == ["Saved"]


def test_feed_filters() -> None:
    user = make_user()
    other_track = TrackFactory(user=user, name="Other")
    a = MatchFactory(
        user=user,
        job=make_job(
            title="Alpha Support",
            company="Zeta Co",
            external_id="1",
            work_type="remote",
            source=SourceFactory(display_name="BoardOne"),
        ),
        score=85,
        verdict="strong",
        track=default_track(user),
    )
    b = MatchFactory(
        user=user,
        job=make_job(title="Beta Agent", company="Yodel", external_id="2", work_type="hybrid"),
        score=45,
        verdict="weak",
        track=other_track,
        status="seen",
    )
    assert titles(user, verdict="strong") == ["Alpha Support"] and titles(user, verdict="weak") == [
        "Beta Agent"
    ]
    assert titles(user, track=other_track.pk) == ["Beta Agent"]
    assert titles(user, work_type="hybrid") == ["Beta Agent"]
    assert titles(user, query="zeta") == ["Alpha Support"] and titles(user, query="BETA") == [
        "Beta Agent"
    ]
    assert titles(user, query="boardone") == ["Alpha Support"]  # by source name
    assert titles(user, new_only=True) == ["Alpha Support"]  # b was already seen
    assert titles(user, status="seen") == ["Beta Agent"] and a and b


def test_eligible_only_excludes_unclear_jobs() -> None:
    user = UserFactory()
    MatchFactory(user=user, job=make_job(title="Global", external_id="1"))
    MatchFactory(
        user=user,
        job=make_job(
            title="Kenya",
            external_id="2",
            is_global=False,
            eligible_countries=["KE"],
            eligibility_known=True,
        ),
    )
    MatchFactory(
        user=user,
        job=make_job(title="Unclear", external_id="3", is_global=False, eligibility_known=False),
    )
    assert sorted(titles(user)) == ["Global", "Kenya", "Unclear"]
    assert sorted(titles(user, eligible_only=True)) == ["Global", "Kenya"]


def test_the_feed_only_contains_the_users_own_matches() -> None:
    mine, theirs = UserFactory(), UserFactory()
    job = make_job()
    MatchFactory(user=mine, job=job)
    MatchFactory(user=theirs, job=make_job(title="Theirs", external_id="x"))
    assert titles(mine) == [job.title] and titles(theirs) == ["Theirs"]


# --- Search now ------------------------------------------------------------------------------------------------------------


def test_search_now_is_limited_to_once_every_ten_minutes(
    django_capture_on_commit_callbacks,
) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    start = datetime.datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    with django_capture_on_commit_callbacks(execute=False):
        services.start_search(user, now=start)
        with pytest.raises(SearchTooSoon) as info:
            services.start_search(user, now=start + datetime.timedelta(minutes=9, seconds=59))
        assert "10 minute" in info.value.message or "minute" in info.value.message
        services.start_search(user, now=start + datetime.timedelta(minutes=10))  # allowed again
    assert UserSettings.objects.get(user=user).last_manual_search_at == start + datetime.timedelta(
        minutes=10
    )


def test_search_now_works_for_users_without_a_settings_row(
    django_capture_on_commit_callbacks,
) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    with django_capture_on_commit_callbacks(execute=False):
        services.start_search(user)
    assert UserSettings.objects.filter(user=user, last_manual_search_at__isnull=False).exists()


def test_the_wait_time_is_reported_in_minutes() -> None:
    from apps.matching.exceptions import SearchTooSoon as Err

    assert "5 minute" in Err(datetime.timedelta(minutes=4, seconds=10)).message
    assert "1 minute" in Err(datetime.timedelta(seconds=5)).message


def test_run_search_queues_only_sources_that_have_not_run_in_five_minutes(
    fake_llm: FakeProvider,
) -> None:
    user = make_user()
    SourceFactory(name="never", last_run_at=None)
    SourceFactory(name="old", last_run_at=timezone.now() - datetime.timedelta(minutes=6))
    SourceFactory(name="recent", last_run_at=timezone.now() - datetime.timedelta(minutes=2))
    SourceFactory(name="off", enabled=False)
    queued: list[int] = []
    services.start_search.__wrapped__ if hasattr(services.start_search, "__wrapped__") else None
    services.run_search(user.pk, enqueue_source=queued.append)
    from apps.sources.models import Source

    assert sorted(Source.objects.filter(pk__in=queued).values_list("name", flat=True)) == [
        "never",
        "old",
    ]


def test_run_search_rescores_recent_stale_and_unscored_matches(fake_llm: FakeProvider) -> None:
    user = make_user()
    ctx = services.build_context(user)
    services.upsert_match(ctx, make_job(title="Support A", external_id="a"))  # type: ignore[arg-type]
    services.upsert_match(
        ctx,
        make_job(
            title="Support B",
            external_id="b",
            first_seen_at=timezone.now() - datetime.timedelta(days=10),
        ),
    )  # type: ignore[arg-type]
    services.run_search(user.pk, enqueue_source=lambda sid: None)
    assert (
        Match.objects.filter(score__isnull=False).count() == 1
    )  # the 10-day-old one is outside the window


def test_run_search_reports_a_used_up_allowance(fake_llm: FakeProvider) -> None:
    user = make_user()
    UsageQuota.objects.create(user=user, monthly_scores=0)
    ctx = services.build_context(user)
    services.upsert_match(ctx, make_job())  # type: ignore[arg-type]
    services.run_search(user.pk, enqueue_source=lambda sid: None)
    assert fake_llm.calls == []


def test_search_progress_over_time(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    assert services.search_progress(user) is None
    source = SourceFactory(name="s1")
    with django_capture_on_commit_callbacks(execute=False):
        start = timezone.now()
        services.start_search(user, now=start)
    from django.core.cache import cache

    cache.set(
        f"search_now:{user.pk}",
        {"started": start.isoformat(), "source_ids": [source.pk], "done": False},
        600,
    )
    running = services.search_progress(user)
    assert (
        running is not None
        and running.active
        and (running.sources_total, running.sources_done) == (1, 0)
    )

    SourceRun.objects.create(
        source=source,
        started_at=start + datetime.timedelta(seconds=1),
        finished_at=timezone.now(),
        success=True,
    )
    MatchFactory(user=user, job=make_job(external_id="n1"))
    cache.set(
        f"search_now:{user.pk}",
        {"started": start.isoformat(), "source_ids": [source.pk], "done": True, "rescored": 3},
        600,
    )
    finished = services.search_progress(user)
    assert finished is not None and not finished.active
    assert (finished.sources_done, finished.new_matches, finished.rescored) == (1, 1, 3)


def test_a_source_that_never_finishes_does_not_poll_forever() -> None:
    user = make_user()
    source = SourceFactory()
    from django.core.cache import cache

    started = timezone.now() - datetime.timedelta(minutes=6)
    cache.set(
        f"search_now:{user.pk}",
        {"started": started.isoformat(), "source_ids": [source.pk], "done": True},
        600,
    )
    progress = services.search_progress(user)
    assert progress is not None and progress.active is False and progress.sources_done == 0


def test_progress_is_per_user() -> None:
    a, b = make_user(), make_user()
    from django.core.cache import cache

    cache.set(
        f"search_now:{a.pk}",
        {"started": timezone.now().isoformat(), "source_ids": [], "done": True},
        600,
    )
    assert services.search_progress(a) is not None and services.search_progress(b) is None


def test_old_unused_helpers_exist() -> None:
    assert services.verdict_for(80) == "strong" and services.verdict_for(79) == "good"
    assert services.verdict_for(59) == "weak" and services.verdict_for(39) == "poor"
    assert time_machine and JobFactory and ExperienceFactory
