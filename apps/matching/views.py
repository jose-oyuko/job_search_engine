"""Thin views for the job feed and match actions (spec §10.5, §10.6)."""

import logging
from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User, UserSettings
from apps.core.tenancy import get_owned_or_404
from apps.jobs.eligibility import is_eligible
from apps.jobs.scam import FLAG_LABELS
from apps.profiles.models import Profile, Track
from apps.tracker import services as tracker_services

from . import services
from .exceptions import NotOwner, SearchTooSoon
from .forms import DismissForm, FeedFilterForm
from .models import Match

logger = logging.getLogger(__name__)

PAGE_SIZE = 20


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


@dataclass
class Card:
    """A match with everything its card shows, worked out once per request."""

    match: Match
    stale: bool
    eligible: bool | None
    flags: list[str]
    limit_reached: bool
    pending: bool


def build_cards(
    user: User, matches: list[Match], *, country: str, version: int, exhausted: bool
) -> list[Card]:
    cards: list[Card] = []
    for match in matches:
        job = match.job
        unscored = match.score is None
        queued = match.scoring_queued_at is not None
        cards.append(
            Card(
                match=match,
                stale=match.is_stale(version),
                eligible=is_eligible(job, country),
                flags=[FLAG_LABELS[f] for f in job.scam_flags if f in FLAG_LABELS],
                limit_reached=unscored and exhausted and not queued,
                pending=unscored and queued and not exhausted,
            )
        )
    return cards


def _user_country(user: User) -> str:
    return UserSettings.objects.for_user(user).values_list("country", flat=True).first() or "KE"


def _profile_version(user: User) -> int:
    profile = Profile.objects.filter(user=user).first()
    return profile.version if profile else 1


def _filters(request: HttpRequest, form: FeedFilterForm) -> services.FeedFilters:
    data = form.cleaned_data if form.is_valid() else {}
    return services.FeedFilters(
        track=data.get("track") or None,
        verdict=data.get("verdict", ""),
        status=data.get("status", ""),
        work_type=data.get("work_type", ""),
        query=(data.get("q") or "").strip(),
        eligible_only=bool(data.get("eligible")),
        new_only=bool(data.get("new")),
    )


def render_feed(request: HttpRequest) -> HttpResponse:
    """The dashboard: the user's ranked matches with filters and "load more"."""
    user = _user(request)
    form = FeedFilterForm(request.GET or None, user=user)
    filters = _filters(request, form)
    paginator = Paginator(services.feed_queryset(user, filters), PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    cards = build_cards(
        user,
        list(page.object_list),
        country=_user_country(user),
        version=_profile_version(user),
        exhausted=services.scores_exhausted(user),
    )
    query = request.GET.copy()
    query.pop("page", None)
    next_url = f"?{query.urlencode()}&page={page.next_page_number()}" if page.has_next() else ""
    next_url = next_url.replace("?&", "?")
    if _is_htmx(request) and request.GET.get("page"):
        return render(
            request,
            "matching/_cards.html",
            {"cards": cards, "next_url": next_url, "dismiss_form": DismissForm()},
        )

    context: dict[str, Any] = {
        "cards": cards,
        "next_url": next_url,
        "form": form,
        "total": paginator.count,
        "has_track": Track.objects.for_user(user).exists(),
        "matchable": services.build_context(user) is not None,
        "filtered": any(
            request.GET.get(k)
            for k in ("track", "verdict", "status", "work_type", "q", "eligible", "new")
        ),
        "progress": services.search_progress(user),
        "dismiss_form": DismissForm(),
    }
    return render(request, "matching/feed.html", context)


# --- actions ----------------------------------------


def _card_for(request: HttpRequest, match: Match) -> HttpResponse:
    user = _user(request)
    match = (
        Match.objects.for_user(user).select_related("job", "job__source", "track").get(pk=match.pk)
    )
    (card,) = build_cards(
        user, [match], country=_user_country(user), version=_profile_version(user),
        exhausted=services.scores_exhausted(user),
    )  # fmt: skip
    return render(request, "matching/_card.html", {"card": card, "dismiss_form": DismissForm()})


def _back(request: HttpRequest) -> HttpResponse:
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts=None) or not target.startswith(
        "/app/"
    ):
        target = reverse("pages:dashboard")
    return redirect(target)


@login_required
@require_POST
def save(request: HttpRequest, pk: int) -> HttpResponse:
    """Toggle saved."""
    user = _user(request)
    match = get_owned_or_404(Match, user, pk=pk)
    new_status = Match.Status.SEEN if match.status == Match.Status.SAVED else Match.Status.SAVED
    services.set_status(user, match, new_status)
    return _card_for(request, match) if _is_htmx(request) else _back(request)


@login_required
@require_POST
def dismiss(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    match = get_owned_or_404(Match, user, pk=pk)
    form = DismissForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    services.set_status(user, match, Match.Status.DISMISSED, reason=reason or "not_relevant")
    return HttpResponse("") if _is_htmx(request) else _back(request)


@login_required
@require_POST
def applied(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark applied: creates the tracker Application (once) and marks the match applied."""
    user = _user(request)
    match = get_owned_or_404(Match, user, pk=pk)
    tracker_services.apply_to_job(user, match.job, match=match)
    return HttpResponse("") if _is_htmx(request) else _back(request)


@login_required
@require_POST
def restore(request: HttpRequest, pk: int) -> HttpResponse:
    """Undo a dismissal or an applied mark."""
    user = _user(request)
    match = get_owned_or_404(Match, user, pk=pk)
    services.set_status(user, match, Match.Status.SEEN)
    return HttpResponse("") if _is_htmx(request) else _back(request)


@login_required
@require_POST
def rescore(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    match = get_owned_or_404(Match, user, pk=pk)
    if services.scores_exhausted(user):
        messages.warning(request, "You have used all of this month's job scoring allowance.")
    else:
        try:
            services.request_rescore(user, match)
        except NotOwner:
            raise  # unreachable: the match was fetched with for_user
        messages.info(request, "Scoring again. This takes a few seconds.")
    return redirect("jobs:detail", pk=match.job_id)


@login_required
@require_GET
def score_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Polled by the job page while a score is being computed; refreshes the page when done."""
    user = _user(request)
    match = get_owned_or_404(Match, user, pk=pk)
    version = _profile_version(user)
    if match.score is not None and match.scored_profile_version == version:
        response = HttpResponse("")
        response["HX-Refresh"] = "true"
        return response
    return render(request, "matching/_scoring_wait.html", {"match": match})


# --- "Search now" ----------------------------------------


@login_required
@require_POST
def search_now(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    error = ""
    try:
        services.start_search(user)
    except SearchTooSoon as exc:
        error = exc.message
    context = {"progress": services.search_progress(user), "error": error}
    if _is_htmx(request):
        return render(request, "matching/_search_status.html", context)
    if error:
        messages.warning(request, error)
    return redirect("pages:dashboard")


@login_required
@require_GET
def search_status(request: HttpRequest) -> HttpResponse:
    context = {"progress": services.search_progress(_user(request)), "error": ""}
    return render(request, "matching/_search_status.html", context)
