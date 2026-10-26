"""Thin views for the kit panel on the job page and ownership-checked downloads (spec §12.7)."""

import logging
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods
from django_ratelimit.decorators import ratelimit
from pydantic import ValidationError

from apps.accounts.models import User
from apps.core.logging import log_event
from apps.jobs.models import Job
from apps.llm.exceptions import QuotaExceeded
from apps.profiles.models import Profile

from . import documents, services
from .exceptions import KitError, NotOwner
from .forms import GenerateForm, parse_edits
from .models import Kit

logger = logging.getLogger(__name__)

# Kit generation spends the user's allowance: 10 an hour per user (spec §20).
kit_limit = ratelimit(group="kit_generate", key="user", rate="10/h", method="POST", block=True)

DOWNLOAD_LABELS = {
    "cv_pdf": "CV (PDF)",
    "cv_docx": "CV (Word)",
    "cover_letter_pdf": "Cover letter (PDF)",
    "cover_letter_docx": "Cover letter (Word)",
}


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _visible_job(user: User, job_id: int) -> Job:
    return get_object_or_404(Job.objects.visible_to(user), pk=job_id)


def panel_context(
    user: User, job: Job, *, selected_id: int | None = None, notice: str = ""
) -> dict[str, Any]:
    """Everything `kits/_panel.html` needs. Also used by the job detail page."""
    kits = services.kit_versions(user, job)
    services.expire_stuck(kits)
    kit = next((k for k in kits if k.pk == selected_id), None) or (kits[0] if kits else None)
    version = Profile.objects.filter(user=user).values_list("version", flat=True).first() or 1

    context: dict[str, Any] = {
        "job": job,
        "kit": kit,
        "kits": kits,
        "notice": notice,
        "exhausted": services.kits_exhausted(user),
        "has_profile": services.has_profile_content(user),
        "generate_form": GenerateForm(),
        "kit_stale": bool(kit and kit.is_stale(version)),
        "downloads": [],
    }
    if kit is not None and kit.is_ready:
        try:
            context["cv"] = documents.build_cv_context(kit)
        except ValidationError:
            context["cv"] = None
        context["letter_paragraphs"] = documents.split_paragraphs(kit.cover_letter_text)
        context["downloads"] = [
            {
                "key": key,
                "label": label,
                "available": bool(getattr(kit, key)),
                "url": reverse("kits:download", args=[kit.pk, key]),
            }
            for key, label in DOWNLOAD_LABELS.items()
        ]
        report = kit.validation_report or {}
        context["report"] = report
        context["issues"] = report.get("issues", [])
        context["notes"] = [*report.get("notes", []), *report.get("letter_warnings", [])]
        context["edit_warnings"] = report.get("edit_warnings", [])
    return context


def _panel_response(
    request: HttpRequest, job: Job, *, selected_id: int | None = None, notice: str = ""
) -> HttpResponse:
    context = panel_context(_user(request), job, selected_id=selected_id, notice=notice)
    if _is_htmx(request):
        return render(request, "kits/_panel.html", context)
    return redirect(f"{reverse('jobs:detail', args=[job.pk])}#kit")


@login_required
@require_GET
def panel(request: HttpRequest, job_id: int) -> HttpResponse:
    """The panel on its own: polled while a kit is being prepared, and used by the version
    dropdown (`?kit=<id>`)."""
    job = _visible_job(_user(request), job_id)
    raw = request.GET.get("kit", "")
    context = panel_context(_user(request), job, selected_id=int(raw) if raw.isdigit() else None)
    return render(request, "kits/_panel.html", context)


@login_required
@require_http_methods(["POST"])
@kit_limit
def generate(request: HttpRequest, job_id: int) -> HttpResponse:
    """Generate a kit, or regenerate with an instruction: queues a new version."""
    user = _user(request)
    job = _visible_job(user, job_id)
    form = GenerateForm(request.POST)
    instruction = form.cleaned_data["instruction"] if form.is_valid() else ""
    notice = ""
    kit = None
    try:
        kit = services.request_kit(user, job, instruction=instruction)
    except QuotaExceeded as exc:
        notice = exc.message
    except KitError as exc:
        notice = exc.message
    return _panel_response(request, job, selected_id=kit.pk if kit else None, notice=notice)


def _owned_kit(request: HttpRequest, pk: int) -> Kit:
    try:
        return services.get_kit(_user(request), pk)
    except NotOwner as exc:
        raise Http404 from exc


@login_required
@require_http_methods(["POST"])
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Save edited text and re-render the files. No LLM call."""
    kit = _owned_kit(request, pk)
    notice = ""
    try:
        warnings = services.apply_edits(_user(request), kit, parse_edits(request.POST))
        notice = (
            "Saved. Some wording breaks the writing style guide; see the notes below."
            if warnings
            else "Saved. Your documents were updated."
        )
    except KitError as exc:
        notice = exc.message
    return _panel_response(request, kit.job, selected_id=kit.pk, notice=notice)


@login_required
@require_http_methods(["POST"])
def rerender(request: HttpRequest, pk: int) -> HttpResponse:
    kit = _owned_kit(request, pk)
    try:
        services.rerender(_user(request), kit)
        notice = "Your documents were rebuilt."
    except KitError as exc:
        notice = exc.message
    return _panel_response(request, kit.job, selected_id=kit.pk, notice=notice)


@login_required
@require_GET
def download(request: HttpRequest, pk: int, key: str) -> FileResponse:
    """Serve one file of the user's own kit as an attachment. Anyone else's kit is a 404."""
    kit = _owned_kit(request, pk)
    if key not in documents.DOWNLOADS:
        raise Http404
    field = getattr(kit, key)
    if not field:
        raise Http404
    try:
        handle = field.open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    _, fmt = documents.DOWNLOADS[key]
    content_type = (
        "application/pdf"
        if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    log_event(logger, logging.INFO, "kit_downloaded", "Kit file downloaded", kit_id=kit.pk, doc=key)
    return FileResponse(
        handle,
        as_attachment=True,
        filename=documents.download_filename(kit, key),
        content_type=content_type,
    )
