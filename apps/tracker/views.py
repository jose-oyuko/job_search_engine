"""Thin views for the kanban board, application detail, stats and manual applications (§14)."""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import User, UserSettings
from apps.jobs.models import Job
from apps.kits.views import DOWNLOAD_LABELS

from . import services
from . import stats as stats_module
from .exceptions import InvalidKit, InvalidStage, NotOwner
from .forms import (
    DetailsForm,
    InterviewForm,
    KitChoiceForm,
    ManualApplicationForm,
    StageForm,
)
from .models import Application, Interview, Stage

logger = logging.getLogger(__name__)


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _tz(user: User) -> str:
    return (
        UserSettings.objects.for_user(user).values_list("timezone", flat=True).first()
        or "Africa/Nairobi"
    )


def _owned(user: User, pk: int) -> Application:
    try:
        return services.get_application(user, pk)
    except NotOwner as exc:
        raise Http404 from exc


def _board_context(user: User) -> dict[str, Any]:
    columns = services.board(user)
    return {
        "columns": [
            {"stage": s.value, "label": s.label, "applications": columns[s.value]} for s in Stage
        ],
        "stage_choices": Stage.choices,
        "total": sum(len(v) for v in columns.values()),
    }


@login_required
@require_GET
def board(request: HttpRequest) -> HttpResponse:
    return render(request, "tracker/board.html", _board_context(_user(request)))


@login_required
@require_POST
def stage(request: HttpRequest, pk: int) -> HttpResponse:
    """Change an application's stage (drag and drop, the mobile dropdown, or the detail page)."""
    user = _user(request)
    application = _owned(user, pk)
    form = StageForm(request.POST)
    if form.is_valid():
        try:
            services.change_stage(
                user, application, form.cleaned_data["stage"], form.cleaned_data["note"]
            )
        except InvalidStage as exc:
            messages.error(request, exc.message)
    else:
        messages.error(request, "That is not a stage.")
    if _is_htmx(request) and request.headers.get("HX-Target") == "board":
        return render(request, "tracker/_board.html", _board_context(user))
    return _back(request, pk)


def _back(request: HttpRequest, pk: int) -> HttpResponse:
    target = request.POST.get("next")
    if target == "board":
        return redirect("tracker:board")
    return redirect("tracker:detail", pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def add(request: HttpRequest) -> HttpResponse:
    """An application for a job found outside the system."""
    user = _user(request)
    form = ManualApplicationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        application = services.add_manual_application(
            user, title=data["title"], company=data["company"], url=data["url"],
            applied_on=data["applied_on"], channel=data["channel"], notes=data["notes"],
        )  # fmt: skip
        messages.success(request, "Application added.")
        return redirect("tracker:detail", pk=application.pk)
    return render(request, "tracker/add.html", {"form": form})


@login_required
@require_POST
def apply_job(request: HttpRequest, job_id: int) -> HttpResponse:
    """Mark applied from a job page (with or without a match)."""
    user = _user(request)
    job = get_object_or_404(Job.objects.visible_to(user), pk=job_id)
    application, created = services.apply_to_job(user, job)
    messages.success(
        request, "Added to your tracker." if created else "This job is already in your tracker."
    )
    return redirect("tracker:detail", pk=application.pk)


def _detail_context(request: HttpRequest, application: Application, **extra: Any) -> dict[str, Any]:
    user = _user(request)
    tz = _tz(user)
    return {
        "application": application,
        "job": application.job,
        "events": application.events.all(),
        "interviews": application.interviews.all(),
        "kits": services.ready_kits(user, application),
        "downloads": [
            {"key": key, "label": label}
            for key, label in DOWNLOAD_LABELS.items()
            if application.kit and getattr(application.kit, key)
        ],
        "stage_choices": Stage.choices,
        "ghost_hint": services.suggests_ghosted(application),
        "details_form": extra.pop("details_form", None)
        or DetailsForm(
            initial={
                "notes": application.notes,
                "channel": application.channel,
                "outcome_reason": application.outcome_reason,
                "salary_offered": application.salary_offered,
            }
        ),
        "interview_form": extra.pop("interview_form", None) or InterviewForm(tz=tz),
        "interview_forms": {
            i.pk: InterviewForm(instance=i, tz=tz, prefix=f"i{i.pk}")
            for i in application.interviews.all()
        },
        "user_tz": tz,
        **extra,
    }


@login_required
@require_GET
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    application = _owned(_user(request), pk)
    return render(request, "tracker/detail.html", _detail_context(request, application))


@login_required
@require_POST
def details(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    application = _owned(user, pk)
    form = DetailsForm(request.POST)
    if form.is_valid():
        services.update_details(user, application, **form.cleaned_data)
        messages.success(request, "Saved.")
        return redirect("tracker:detail", pk=pk)
    return render(
        request,
        "tracker/detail.html",
        _detail_context(request, application, details_form=form),
        status=400,
    )


@login_required
@require_POST
def choose_kit(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    application = _owned(user, pk)
    form = KitChoiceForm(request.POST)
    kit_id = form.cleaned_data["kit"] if form.is_valid() else None
    try:
        services.set_kit(user, application, kit_id)
        messages.success(request, "Kit updated.")
    except InvalidKit as exc:
        messages.error(request, exc.message)
    return redirect("tracker:detail", pk=pk)


@login_required
@require_POST
def interview_add(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    application = _owned(user, pk)
    form = InterviewForm(request.POST, tz=_tz(user))
    if form.is_valid():
        services.add_interview(user, application, **form.cleaned_data)
        messages.success(request, "Interview added.")
        return redirect("tracker:detail", pk=pk)
    return render(
        request,
        "tracker/detail.html",
        _detail_context(request, application, interview_form=form),
        status=400,
    )


def _owned_interview(user: User, pk: int) -> Interview:
    try:
        return services.get_interview(user, pk)
    except NotOwner as exc:
        raise Http404 from exc


@login_required
@require_POST
def interview_edit(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    interview = _owned_interview(user, pk)
    form = InterviewForm(request.POST, instance=interview, tz=_tz(user), prefix=f"i{pk}")
    if form.is_valid():
        services.update_interview(user, interview, **form.cleaned_data)
        messages.success(request, "Interview saved.")
    else:
        messages.error(request, "That interview could not be saved. Check the date.")
    return redirect("tracker:detail", pk=interview.application_id)


@login_required
@require_POST
def interview_delete(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    interview = _owned_interview(user, pk)
    application_id = interview.application_id
    services.delete_interview(user, interview)
    messages.success(request, "Interview removed.")
    return redirect("tracker:detail", pk=application_id)


@login_required
@require_GET
def stats(request: HttpRequest) -> HttpResponse:
    result = stats_module.compute(_user(request))
    groups = [
        ("By where you found it", result.by_source),
        ("By job target", result.by_track),
        ("With and without a kit", result.by_kit),
    ]
    return render(request, "tracker/stats.html", {"stats": result, "groups": groups})
