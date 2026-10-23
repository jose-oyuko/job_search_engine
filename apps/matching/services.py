"""Matching: rule filter, LLM scoring, the feed query and "Search now" (spec §10).

Views, tasks and the manual-input hook only call into here. Every query on matches is scoped to
the acting user; logs carry IDs and counts only, never job or profile text.
"""

import datetime
import functools
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from apps.accounts.models import User, UserSettings
from apps.core.logging import log_event
from apps.jobs.models import Job
from apps.llm.client import call_structured
from apps.llm.exceptions import LLMError, QuotaExceeded
from apps.llm.models import LLMCall
from apps.llm.schemas import MatchScore
from apps.llm.services import check_quota
from apps.llm.untrusted import MAX_JOB_CHARS, wrap_untrusted
from apps.profiles.models import Experience, Profile, Skill, Track
from apps.profiles.serializers import profile_summary
from apps.sources.models import Source, SourceRun

from . import hooks, rules
from .exceptions import NotOwner, SearchTooSoon
from .models import Match

logger = logging.getLogger(__name__)

RECENT_DAYS = 14  # how far back a re-match looks (spec §10.1)
SEARCH_RECENT_DAYS = 7  # how far back "Search now" rescoring looks (spec §10.6)
SEARCH_COOLDOWN = datetime.timedelta(minutes=10)
SOURCE_MIN_AGE = datetime.timedelta(minutes=5)
SCORE_BATCH_LIMIT = 40
FIRST_PASS_SCORE_LIMIT = 10
MAX_WORDS = {"strength": 15, "gap": 15, "summary": 25}
_SEARCH_CACHE_SECONDS = 30 * 60

SCORED = "scored"
BLOCKED = "blocked"  # the user's scoring allowance is used up; no LLM call was made
FAILED = "failed"
SKIPPED = "skipped"


# --- who can be matched ----------------------------------------


@dataclass
class UserContext:
    """Everything the rule filter needs about one user, loaded once per batch."""

    user: User
    country: str
    tracks: list[Track]
    skill_names: list[str]
    profile_version: int


def build_context(user: User) -> UserContext | None:
    """The user's matching inputs, or None if they cannot be matched yet (no job target, or an
    empty profile). Users with no track get no matches (spec §10.2)."""
    if not user.is_active:
        return None
    tracks = list(Track.objects.for_user(user).order_by("-is_default", "name"))
    if not tracks:
        return None
    profile = Profile.objects.filter(user=user).first()
    skills = list(Skill.objects.for_user(user).values_list("name", flat=True))
    has_profile = (
        bool(profile and (profile.headline or profile.summary))
        or skills
        or Experience.objects.for_user(user).exists()
    )
    if not has_profile:
        return None
    country = UserSettings.objects.for_user(user).values_list("country", flat=True).first() or "KE"
    return UserContext(
        user=user,
        country=country,
        tracks=tracks,
        skill_names=skills,
        profile_version=profile.version if profile else 1,
    )


def pick_track(job: Job, ctx: UserContext) -> tuple[Track, rules.RuleResult]:
    """Evaluate the job against every track. The best passing track (most keyword and skill hits,
    the default track on ties) wins; if none passes, the best-scoring failure is returned."""
    results = [
        (track, rules.evaluate(job, track, country=ctx.country, skill_names=ctx.skill_names))
        for track in ctx.tracks
    ]
    passing = [pair for pair in results if pair[1].passed]
    pool = passing or results
    return max(pool, key=lambda pair: pair[1].relevance)


# --- creating and updating matches ----------------------------------------


def upsert_match(
    ctx: UserContext, job: Job, *, force: bool = False, existing: Match | None = None
) -> tuple[Match | None, bool]:
    """Apply the rule filter and create or update the user's match for `job`.

    A job that fails the rules gets no match, unless `force` (the user added it themselves) or the
    user already saved or applied to it. Returns `(match, created)`.
    """
    track, result = pick_track(job, ctx)
    if existing is None:
        existing = Match.objects.for_user(ctx.user).filter(job=job).first()
    if not result.passed and not force:
        if existing is None:
            return None, False
        if existing.status in (Match.Status.SAVED, Match.Status.APPLIED):
            _update_rules(existing, track, result)
            return existing, False
        existing.delete()  # no longer relevant (for example the track changed)
        return None, False
    if existing is None:
        match = Match.objects.create(
            user=ctx.user,
            job=job,
            track=track,
            rule_pass=result.passed,
            rule_reasons=result.reasons,
        )
        return match, True
    _update_rules(existing, track, result)
    return existing, False


def _update_rules(match: Match, track: Track, result: rules.RuleResult) -> None:
    match.track = track
    match.rule_pass = result.passed
    match.rule_reasons = result.reasons
    match.save(update_fields=["track", "rule_pass", "rule_reasons", "updated_at"])


def match_job_for_user(user: User, job: Job) -> Match | None:
    """Match a job the user added themselves (spec §10.1): shown even if the rules say no, and
    scored in the background. This is the function `apps.jobs.matching_hook` looks for."""
    ctx = build_context(user)
    if ctx is None:
        return None
    match, created = upsert_match(ctx, job, force=True)
    if match is not None and match.score is None:
        queue_scoring([match.pk])
    log_event(
        logger, logging.INFO, "manual_match_created", "Match for a manual job",
        match_id=match.pk if match else None, was_created=created,
    )  # fmt: skip
    return match


def match_new_job(job_id: int) -> list[int]:
    """Run the rule filter for every matchable user and queue scoring for the new matches.

    Only public, active, canonical jobs are matched this way; duplicates are never matched and
    private jobs belong to their owner's own flow. Returns the IDs of the matches created.
    """
    job = Job.objects.filter(pk=job_id).first()
    if (
        job is None
        or job.duplicate_of_id is not None
        or job.visibility != Job.Visibility.PUBLIC
        or job.status != Job.Status.ACTIVE
    ):
        return []
    created_ids: list[int] = []
    evaluated = 0
    candidates = User.objects.filter(is_active=True, pk__in=Track.objects.values("user_id"))
    for user in candidates:
        ctx = build_context(user)
        if ctx is None:
            continue
        evaluated += 1
        match, created = upsert_match(ctx, job)
        if match is not None and created:
            created_ids.append(match.pk)
    queue_scoring(created_ids)
    log_event(
        logger, logging.INFO, "match_batch_finished", "Job matched against users",
        job_id=job.pk, users=evaluated, matched=len(created_ids),
    )  # fmt: skip
    return created_ids


@dataclass(frozen=True)
class RematchResult:
    created: int
    updated: int
    removed: int


def rematch_user(user_id: int) -> RematchResult:
    """Re-run the rule filter over the last 14 days of jobs after a profile or track change.

    Scores are not recomputed here (cost); a score computed from an older profile version simply
    shows as stale and is refreshed when viewed or on "Rescore" (spec §10.1).
    """
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return RematchResult(0, 0, 0)
    ctx = build_context(user)
    if ctx is None:
        removed = (
            Match.objects.for_user(user)
            .exclude(status__in=[Match.Status.SAVED, Match.Status.APPLIED])
            .delete()[0]
        )
        return RematchResult(0, 0, removed)
    cutoff = timezone.now() - datetime.timedelta(days=RECENT_DAYS)
    jobs = list(Job.objects.visible_to(user).canonical().active().filter(first_seen_at__gte=cutoff))
    existing = {m.job_id: m for m in Match.objects.for_user(user).filter(job__in=jobs)}
    created = updated = removed = 0
    for job in jobs:
        before = existing.get(job.pk)
        match, was_created = upsert_match(ctx, job, existing=before)
        if match is None and before is not None:
            removed += 1
        elif was_created:
            created += 1
        elif match is not None:
            updated += 1
    if created and not Match.objects.for_user(user).filter(score__isnull=False).exists():
        # A first pass for a new user: score a few so the dashboard is not empty (spec §15.3).
        score_pending_for_user(user, days=RECENT_DAYS, limit=FIRST_PASS_SCORE_LIMIT)
    log_event(
        logger, logging.INFO, "rematch_finished", "User re-matched",
        user_id_for=user.pk, created=created, updated=updated, removed=removed,
    )  # fmt: skip
    return RematchResult(created, updated, removed)


# --- scoring ----------------------------------------


def queue_scoring(match_ids: list[int]) -> None:
    """Queue a scoring task per match, after the surrounding transaction commits."""
    if not match_ids:
        return
    from apps.core.tasks import enqueue

    from .tasks import score_match_task

    now = timezone.now()
    Match.objects.filter(pk__in=match_ids).update(scoring_queued_at=now)
    for match_id in match_ids:
        transaction.on_commit(functools.partial(enqueue, score_match_task, match_id))


def scores_exhausted(user: User) -> bool:
    """True when the user's monthly scoring allowance (or spend cap) is used up."""
    try:
        check_quota(user, LLMCall.Task.SCORE_MATCH)
    except QuotaExceeded:
        return True
    return False


def _clip_words(text: str, limit: int) -> str:
    words = text.split()
    return " ".join(words[:limit]) + ("…" if len(words) > limit else "")


def verdict_for(score: int) -> str:
    if score >= 80:
        return Match.Verdict.STRONG
    if score >= 60:
        return Match.Verdict.GOOD
    if score >= 40:
        return Match.Verdict.WEAK
    return Match.Verdict.POOR


def build_scoring_input(match: Match, profile_version: int) -> tuple[str, str]:
    """The two prompt blocks: the candidate (a cached deterministic summary plus the track) and the
    job (its extracted facts when available, otherwise title and text). Both are wrapped as
    untrusted data."""
    user = match.user
    lines = [profile_summary(user)]
    track = match.track
    if track is not None:
        lines.append(f"Target track: {track.name}")
        if track.target_titles:
            lines.append("Target roles: " + ", ".join(track.target_titles))
        if track.work_types:
            lines.append("Wants: " + ", ".join(track.work_types))
        if track.min_pay_amount is not None:
            lines.append(
                f"Minimum pay: {track.min_pay_amount} {track.min_pay_currency} "
                f"per {track.min_pay_period}"
            )
    candidate = wrap_untrusted("candidate", "\n".join(lines), 6000)

    job = match.job
    facts = [f"Title: {job.title}", f"Company: {job.company}"]
    if job.pay_text:
        facts.append(f"Pay: {job.pay_text}")
    if job.locations_raw:
        facts.append("Open to: " + ", ".join(job.locations_raw))
    facts.append(f"Work type: {job.work_type}")
    extracted = job.extracted or {}
    if extracted:
        for label, key in (
            ("Summary", "summary"),
            ("Seniority", "seniority"),
            ("Hours", "hours_commitment"),
        ):
            if extracted.get(key):
                facts.append(f"{label}: {extracted[key]}")
        for label, key in (
            ("Required skills", "required_skills"),
            ("Nice to have", "nice_to_have_skills"),
            ("Responsibilities", "responsibilities"),
            ("Requirements", "requirements"),
        ):
            values = extracted.get(key)
            if isinstance(values, list) and values:
                facts.append(f"{label}: " + "; ".join(str(v) for v in values))
    else:
        facts.append("\n" + job.description_text)
    return candidate, wrap_untrusted("job_posting", "\n".join(facts), MAX_JOB_CHARS)


def score_match(match_id: int) -> str:
    """Score one match with the LLM and store the result. Returns SCORED, BLOCKED (allowance used
    up; no model call), FAILED or SKIPPED. Idempotent: an up-to-date score is left alone."""
    match = Match.objects.select_related("job", "track", "user").filter(pk=match_id).first()
    if match is None:
        return SKIPPED
    user = match.user
    profile = Profile.objects.filter(user=user).first()
    version = profile.version if profile else 1
    if match.score is not None and match.scored_profile_version == version:
        return SKIPPED
    if scores_exhausted(user):
        log_event(
            logger, logging.INFO, "score_blocked", "Scoring allowance used up", match_id=match.pk
        )
        return BLOCKED

    candidate, job_block = build_scoring_input(match, version)
    digest = hashlib.sha256(f"{candidate}\n{job_block}".encode()).hexdigest()[:32]
    try:
        result = call_structured(
            task=LLMCall.Task.SCORE_MATCH,
            prompt_name="score_match",
            variables={"candidate": candidate, "job": job_block},
            schema=MatchScore,
            user=user,
            cache_key=f"score:{user.pk}:{digest}",
        )
    except QuotaExceeded:
        return BLOCKED
    except LLMError:
        log_event(
            logger, logging.ERROR, "score_failed", "Scoring failed",
            match_id=match.pk, exc_info=True,
        )  # fmt: skip
        return FAILED

    score = max(0, min(100, int(result.score)))
    match.score = score
    match.verdict = result.verdict if result.verdict == verdict_for(score) else verdict_for(score)
    match.strengths = [
        _clip_words(s, MAX_WORDS["strength"]) for s in result.strengths if s.strip()
    ][:4]
    match.gaps = [_clip_words(g, MAX_WORDS["gap"]) for g in result.gaps if g.strip()][:4]
    match.summary = _clip_words(result.summary, MAX_WORDS["summary"])[:300]
    match.scored_profile_version = version
    match.scored_at = timezone.now()
    match.llm_model = settings.LLM_MODEL_FAST or f"{settings.LLM_PROVIDER}-fast"
    match.scoring_queued_at = None
    match.save()
    hooks.after_score(match)
    log_event(
        logger, logging.INFO, "match_scored", "Match scored",
        match_id=match.pk, score=score, verdict=match.verdict,
    )  # fmt: skip
    return SCORED


@dataclass(frozen=True)
class ScoringSummary:
    scored: int
    blocked: bool


def pending_scores(user: User, *, days: int) -> QuerySet[Match]:
    """Rule-passing matches from the last `days` that are unscored or stale, newest jobs first."""
    profile = Profile.objects.filter(user=user).first()
    version = profile.version if profile else 1
    cutoff = timezone.now() - datetime.timedelta(days=days)
    return (
        Match.objects.for_user(user)
        .filter(rule_pass=True, job__first_seen_at__gte=cutoff)
        .exclude(status=Match.Status.DISMISSED)
        .filter(Q(score__isnull=True) | ~Q(scored_profile_version=version))
        .order_by("-job__first_seen_at")
    )


def score_pending_for_user(
    user: User, *, days: int, limit: int = SCORE_BATCH_LIMIT
) -> ScoringSummary:
    """Score a user's unscored and stale matches until the limit or their allowance runs out."""
    scored = 0
    for match in list(pending_scores(user, days=days)[:limit]):
        outcome = score_match(match.pk)
        if outcome == BLOCKED:
            return ScoringSummary(scored, True)
        scored += outcome == SCORED
    return ScoringSummary(scored, False)


def request_rescore(user: User, match: Match) -> None:
    """The "Rescore" action: queue a fresh score even if the current one is up to date."""
    if match.user_id != user.pk:
        raise NotOwner
    Match.objects.filter(pk=match.pk).update(
        score=None, scored_profile_version=None, scoring_queued_at=timezone.now()
    )
    queue_scoring([match.pk])


def ensure_scoring_queued(user: User, match: Match, profile_version: int) -> bool:
    """Lazy refresh when a match is viewed: queue scoring if it is unscored or stale and has not
    been queued recently. Returns True if scoring is now pending."""
    if match.user_id != user.pk:
        raise NotOwner
    needs = match.score is None or match.scored_profile_version != profile_version
    if not needs:
        return False
    queued = match.scoring_queued_at
    if queued is not None and queued > timezone.now() - datetime.timedelta(minutes=10):
        return True  # already on its way
    if scores_exhausted(user):
        return False
    queue_scoring([match.pk])
    return True


# --- statuses ----------------------------------------


def set_status(user: User, match: Match, status: str, *, reason: str = "") -> Match:
    """Save, dismiss, mark applied or reset a match. Dismissing records why (for tuning)."""
    if match.user_id != user.pk:
        raise NotOwner
    valid = {s.value for s in Match.Status}
    if status not in valid:
        raise ValueError("unknown status")
    match.status = status
    match.dismiss_reason = reason if status == Match.Status.DISMISSED else ""
    match.save(update_fields=["status", "dismiss_reason", "updated_at"])
    return match


def mark_seen(match: Match) -> None:
    """New or notified becomes seen. Conditional in the database, so a status the user set in the
    meantime (saved, dismissed...) is never overwritten."""
    Match.objects.filter(pk=match.pk, status__in=[Match.Status.NEW, Match.Status.NOTIFIED]).update(
        status=Match.Status.SEEN
    )


# --- the feed ----------------------------------------


@dataclass
class FeedFilters:
    track: int | None = None
    verdict: str = ""
    status: str = ""  # "" = the default view
    work_type: str = ""
    query: str = ""
    eligible_only: bool = False
    new_only: bool = False


def feed_queryset(user: User, f: FeedFilters) -> QuerySet[Match]:
    """The user's matches for the dashboard: ranked by score (unscored last), then newest job."""
    queryset = (
        Match.objects.for_user(user)
        .select_related("job", "job__source", "track")
        .filter(job__duplicate_of__isnull=True)
    )
    if f.status:
        queryset = queryset.filter(status=f.status)
    else:
        # Default: matches that passed the rules (or that the user saved), minus dismissed/applied.
        queryset = queryset.filter(Q(rule_pass=True) | Q(status=Match.Status.SAVED)).exclude(
            status__in=[Match.Status.DISMISSED, Match.Status.APPLIED]
        )
    if f.track:
        queryset = queryset.filter(track_id=f.track)
    if f.verdict:
        queryset = queryset.filter(verdict=f.verdict)
    if f.work_type:
        queryset = queryset.filter(job__work_type=f.work_type)
    if f.query:
        queryset = queryset.filter(
            Q(job__title__icontains=f.query)
            | Q(job__company__icontains=f.query)
            | Q(job__source__display_name__icontains=f.query)
        )
    if f.new_only:
        queryset = queryset.filter(status__in=[Match.Status.NEW, Match.Status.NOTIFIED])
    if f.eligible_only:
        queryset = queryset.filter(job__eligibility_known=True).exclude(
            job__is_global=False, job__eligible_countries=[], job__eligible_regions=[]
        )
    return queryset.order_by(F("score").desc(nulls_last=True), "-job__first_seen_at", "-id")


# --- "Search now" (spec §10.6) ----------------------------------------


def _search_key(user: User) -> str:
    return f"search_now:{user.pk}"


def start_search(user: User, *, now: datetime.datetime | None = None) -> datetime.datetime:
    """Begin a manual search. Raises `SearchTooSoon` if one ran in the last 10 minutes. Returns the
    start time; the work itself runs in `run_search` on the worker."""
    now = now or timezone.now()
    with transaction.atomic():
        row, _ = UserSettings.objects.select_for_update().get_or_create(user=user)
        last = row.last_manual_search_at
        if last is not None and now - last < SEARCH_COOLDOWN:
            raise SearchTooSoon(SEARCH_COOLDOWN - (now - last))
        row.last_manual_search_at = now
        row.save(update_fields=["last_manual_search_at", "updated_at"])
    cache.set(
        _search_key(user),
        {"started": now.isoformat(), "source_ids": [], "done": False},
        _SEARCH_CACHE_SECONDS,
    )
    from apps.core.tasks import enqueue

    from .tasks import search_now_task

    transaction.on_commit(functools.partial(enqueue, search_now_task, user.pk))
    return now


def run_search(user_id: int, *, enqueue_source: Callable[[int], Any] | None = None) -> None:
    """Queue a run for every enabled source not run in the last 5 minutes, then rescore the user's
    recent stale and unscored matches within their allowance."""
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return
    state: dict[str, Any] = cache.get(_search_key(user)) or {
        "started": timezone.now().isoformat(),
        "source_ids": [],
        "done": False,
    }
    if enqueue_source is None:
        from apps.core.tasks import enqueue
        from apps.sources.tasks import run_source_task

        def enqueue_source(source_id: int) -> Any:
            return enqueue(run_source_task, source_id, SourceRun.Trigger.MANUAL)

    stale_before = timezone.now() - SOURCE_MIN_AGE
    source_ids: list[int] = []
    for source in Source.objects.filter(enabled=True):
        if source.last_run_at is None or source.last_run_at <= stale_before:
            enqueue_source(source.pk)
            source_ids.append(source.pk)
    state["source_ids"] = source_ids
    cache.set(_search_key(user), state, _SEARCH_CACHE_SECONDS)

    summary = score_pending_for_user(user, days=SEARCH_RECENT_DAYS)
    state.update(done=True, rescored=summary.scored, blocked=summary.blocked)
    cache.set(_search_key(user), state, _SEARCH_CACHE_SECONDS)
    log_event(
        logger, logging.INFO, "search_now_finished", "Search now finished",
        sources=len(source_ids), rescored=summary.scored, blocked=summary.blocked,
    )  # fmt: skip


@dataclass(frozen=True)
class SearchProgress:
    active: bool
    sources_total: int
    sources_done: int
    new_matches: int
    rescored: int
    blocked: bool


def search_progress(user: User) -> SearchProgress | None:
    """Where a running or just-finished manual search is, or None if there is none to show."""
    state: dict[str, Any] | None = cache.get(_search_key(user))
    if not state:
        return None
    started = datetime.datetime.fromisoformat(state["started"])
    source_ids: list[int] = state.get("source_ids", [])
    done_sources = (
        SourceRun.objects.filter(
            source_id__in=source_ids, finished_at__isnull=False, started_at__gte=started
        )
        .values("source_id")
        .distinct()
        .count()
    )
    new_matches = Match.objects.for_user(user).filter(created_at__gte=started).count()
    finished = bool(state.get("done")) and done_sources >= len(source_ids)
    # A source that never finishes must not leave the page polling forever.
    timed_out = timezone.now() - started > datetime.timedelta(minutes=5)
    return SearchProgress(
        active=not (finished or timed_out),
        sources_total=len(source_ids),
        sources_done=done_sources,
        new_matches=new_matches,
        rescored=int(state.get("rescored", 0)),
        blocked=bool(state.get("blocked", False)),
    )
