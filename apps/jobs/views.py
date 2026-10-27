"""Thin views for adding a job by URL or pasted text, and a simple job detail page (spec §8)."""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit.decorators import ratelimit

from apps.accounts.models import User, UserSettings
from apps.core.logging import log_event
from apps.kits.views import panel_context as kit_panel_context
from apps.llm.exceptions import QuotaExceeded
from apps.matching import services as matching_services
from apps.matching.forms import DismissForm
from apps.matching.models import Match
from apps.profiles.models import Profile
from apps.tracker import services as tracker_services

from . import manual
from .eligibility import is_eligible
from .forms import ReportForm, TextForm, UrlForm
from .manual import ManualJobError, NeedsDetails
from .models import Job, JobReport
from .scam import FLAG_LABELS

logger = logging.getLogger(__name__)

# One shared allowance of 20 additions a day across both tabs (spec §20).
manual_limit = ratelimit(group="manual_add", key="user", rate="20/d", method="POST", block=True)


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def _render_add(
    request: HttpRequest,
    *,
    tab: str,
    url_form: UrlForm | None = None,
    text_form: TextForm | None = None,
    status: int = 200,
) -> HttpResponse:
    recent = Job.objects.filter(owner=_user(request), origin=Job.Origin.MANUAL_TEXT)[:5]
    context = {
        "tab": tab,
        "url_form": url_form or UrlForm(),
        "text_form": text_form or TextForm(),
        "recent": recent,
        "blocked_sites": ["LinkedIn", "Indeed", "Glassdoor"],
    }
    return render(request, "jobs/add.html", context, status=status)


@login_required
@require_GET
def add(request: HttpRequest) -> HttpResponse:
    tab = "text" if request.GET.get("tab") == "text" else "url"
    return _render_add(request, tab=tab)


@login_required
@require_http_methods(["POST"])
@manual_limit
def add_url(request: HttpRequest) -> HttpResponse:
    form = UrlForm(request.POST)
    if form.is_valid():
        try:
            result = manual.add_job_from_url(_user(request), form.cleaned_data["url"])
        except ManualJobError as exc:
            form.add_error("url", exc.message)
        except QuotaExceeded as exc:
            form.add_error(None, exc.message)
        else:
            return _added(request, result)
    return _render_add(request, tab="url", url_form=form, status=400)


@login_required
@require_http_methods(["POST"])
@manual_limit
def add_text(request: HttpRequest) -> HttpResponse:
    form = TextForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            result = manual.add_job_from_text(
                _user(request),
                data["text"],
                title=data["title"],
                company=data["company"],
                url=data["url"],
            )
        except NeedsDetails as exc:
            for field in exc.missing:
                form.add_error(field, "Please fill this in.")
            form.add_error(None, exc.message)
        except ManualJobError as exc:
            form.add_error("url" if exc.reason == "bad_link" else "text", exc.message)
        except QuotaExceeded as exc:
            form.add_error(None, exc.message)
        else:
            return _added(request, result)
    return _render_add(request, tab="text", text_form=form, status=400)


def _added(request: HttpRequest, result: manual.ManualResult) -> HttpResponse:
    if not result.created:
        messages.info(request, "We already had this job, so here it is.")
    return redirect("jobs:detail", pk=result.job.pk)


@login_required
@require_GET
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Public jobs and the user's own private jobs; anyone else's private job is a 404."""
    user = _user(request)
    job = get_object_or_404(Job.objects.visible_to(user), pk=pk)
    canonical_id = job.duplicate_of_id
    if canonical_id is not None and Job.objects.visible_to(user).filter(pk=canonical_id).exists():
        return redirect("jobs:detail", pk=canonical_id)
    country = UserSettings.objects.for_user(user).values_list("country", flat=True).first() or "KE"

    # The user's own match, if any: score, strengths and gaps, and why it was shown.
    match = Match.objects.for_user(user).select_related("track").filter(job=job).first()
    version = Profile.objects.filter(user=user).values_list("version", flat=True).first() or 1
    scoring_pending = False
    if match is not None:
        matching_services.mark_seen(match)
        # Lazy refresh: an unscored or stale match is scored when it is viewed (spec §10.1).
        scoring_pending = matching_services.ensure_scoring_queued(user, match, version)
    context: dict[str, Any] = {
        "job": job,
        "country": country,
        "eligible": is_eligible(job, country),
        "flags": [FLAG_LABELS.get(f, f) for f in job.scam_flags if f in FLAG_LABELS],
        "extracted": job.extracted or {},
        "match": match,
        "stale": match is not None and match.is_stale(version),
        "scoring_pending": scoring_pending and match is not None and match.score is None,
        "scores_exhausted": match is not None and matching_services.scores_exhausted(user),
        "application": tracker_services.application_for_job(user, job),
        "report_form": ReportForm(),
        "dismiss_form": DismissForm(),
        "alternate_urls": [u for u in job.alternate_urls if isinstance(u, str)][:5],
        **kit_panel_context(user, job),
    }
    log_event(logger, logging.DEBUG, "job_viewed", "Job viewed", job_id=job.pk)
    return render(request, "jobs/detail.html", context)


@login_required
@require_http_methods(["POST"])
def report(request: HttpRequest, pk: int) -> HttpResponse:
    """ "Report this listing" (spec §5.5): scam, expired, wrong information or other."""
    user = _user(request)
    job = get_object_or_404(Job.objects.visible_to(user), pk=pk)
    form = ReportForm(request.POST)
    if form.is_valid():
        JobReport.objects.update_or_create(
            user=user,
            job=job,
            defaults={"reason": form.cleaned_data["reason"], "note": form.cleaned_data["note"]},
        )
        log_event(
            logger,
            logging.INFO,
            "job_reported",
            "Job reported",
            job_id=job.pk,
            reason=form.cleaned_data["reason"],
        )
        messages.success(request, "Thanks. We will take a look at this listing.")
    else:
        messages.error(request, "Please choose a reason for the report.")
    return redirect("jobs:detail", pk=job.pk)
