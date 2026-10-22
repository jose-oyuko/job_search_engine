"""Thin views for the profile editor, tracks and CV import. Logic lives in `services`.

The editor is HTMX-driven: each item is a `<div id="item-<section>-<pk>">` that is swapped in place
between its display and edit states. Forms post to the server, which answers with the HTML
fragment to put back. Without HTMX the endpoints just send the browser to the profile page.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.accounts.models import User
from apps.core.logging import log_event
from apps.core.tenancy import get_owned_or_404

from . import services
from .exceptions import ImportStateError, InvalidCVFile, NotOwner, ProfileError, UnreadableCV
from .forms import CVUploadForm, ProfileBasicsForm, ReviewBasicsForm, TrackForm
from .models import (
    Certification,
    CVImport,
    Education,
    Experience,
    Project,
    Skill,
    Track,
)
from .sections import SECTIONS, Section

logger = logging.getLogger(__name__)


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _section(slug: str) -> Section:
    try:
        return SECTIONS[slug]
    except KeyError:
        raise Http404 from None


def _m2m(form: Any) -> dict[str, Any]:
    return {k: form.cleaned_data[k] for k in ("skills", "technologies") if k in form.cleaned_data}


def _scope_id(section: Section, obj: Any) -> int | None:
    return obj.experience_id if section.parent_field else None


def _item_context(section: Section, obj: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "section": section,
        "obj": obj,
        "bullet_section": SECTIONS["bullet"],
    }
    if section.slug == "experience":
        context["bullets"] = list(obj.bullets.all())
    return context


def _render_item(request: HttpRequest, section: Section, obj: Any) -> HttpResponse:
    return render(request, "profiles/_item.html", _item_context(section, obj))


def _render_form(
    request: HttpRequest,
    section: Section,
    form: Any,
    obj: Any = None,
    scope_id: int | None = None,
) -> HttpResponse:
    context = {"section": section, "form": form, "obj": obj, "scope_id": scope_id}
    return render(request, "profiles/_item_form.html", context)


# --- the profile page ----------------------------------------------


@login_required
@require_GET
def profile_page(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    profile = services.get_profile(user)
    experiences = (
        Experience.objects.for_user(user)
        .prefetch_related("bullets__skills")
        .order_by("order", "id")
    )
    context = {
        "profile": profile,
        "completeness": services.completeness(user),
        "experiences": experiences,
        "skills": Skill.objects.for_user(user),
        "projects": Project.objects.for_user(user).prefetch_related("technologies"),
        "education": Education.objects.for_user(user),
        "certifications": Certification.objects.for_user(user),
        "sections": SECTIONS,
        "bullet_section": SECTIONS["bullet"],
        "has_tracks": Track.objects.for_user(user).exists(),
    }
    return render(request, "profiles/profile.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def basics(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    profile = services.get_profile(user)
    form = ProfileBasicsForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save(commit=False)
        services.save_profile(user, profile)
        messages.success(request, "Profile details saved.")
        return redirect("profiles:profile")
    return render(request, "profiles/basics.html", {"form": form})


# --- generic inline CRUD ----------------------------------------------


@login_required
@require_GET
def item_new(request: HttpRequest, section: str) -> HttpResponse:
    sec = _section(section)
    if not _is_htmx(request):
        return redirect("profiles:profile")
    user = _user(request)
    initial: dict[str, Any] = {}
    scope_id: int | None = None
    if sec.parent_field:
        parent = get_owned_or_404(Experience, user, pk=request.GET.get("experience", 0))
        initial[sec.parent_field] = parent.pk
        scope_id = parent.pk
    if request.GET.get("cancel"):
        # Put back the empty placeholder the "Add" button swaps the form into.
        return HttpResponse(f'<div id="{sec.new_id(scope_id)}"></div>')
    return _render_form(request, sec, sec.form(initial=initial, user=user), scope_id=scope_id)


@login_required
@require_POST
def item_create(request: HttpRequest, section: str) -> HttpResponse:
    sec = _section(section)
    user = _user(request)
    form = sec.form(request.POST, user=user)
    scope_id: int | None = None
    if sec.parent_field:
        # A missing or foreign parent is not a form error the user can fix: refuse it.
        scope_id = get_owned_or_404(Experience, user, pk=request.POST.get(sec.parent_field, 0)).pk
    if not form.is_valid():
        return _render_form(request, sec, form, scope_id=scope_id)
    obj = form.save(commit=False)
    try:
        services.save_item(user, obj, m2m=_m2m(form))
    except NotOwner:
        raise Http404 from None
    if not _is_htmx(request):
        return redirect("profiles:profile")
    response = _render_item(request, sec, obj)
    # The form lives in a "new" container; put the saved row at the end of the list instead and
    # clear the container out-of-band.
    response.content += f'<div id="{sec.new_id(scope_id)}" hx-swap-oob="true"></div>'.encode()
    response["HX-Retarget"] = f"#{sec.list_id(scope_id)}"
    response["HX-Reswap"] = "beforeend"
    return response


@login_required
@require_GET
def item_show(request: HttpRequest, section: str, pk: int) -> HttpResponse:
    sec = _section(section)
    obj = get_owned_or_404(sec.model, _user(request), pk=pk)
    if not _is_htmx(request):
        return redirect("profiles:profile")
    return _render_item(request, sec, obj)


@login_required
@require_GET
def item_edit(request: HttpRequest, section: str, pk: int) -> HttpResponse:
    sec = _section(section)
    user = _user(request)
    obj = get_owned_or_404(sec.model, user, pk=pk)
    if not _is_htmx(request):
        return redirect("profiles:profile")
    return _render_form(request, sec, sec.form(instance=obj, user=user), obj=obj)


@login_required
@require_POST
def item_save(request: HttpRequest, section: str, pk: int) -> HttpResponse:
    sec = _section(section)
    user = _user(request)
    obj = get_owned_or_404(sec.model, user, pk=pk)
    form = sec.form(request.POST, instance=obj, user=user)
    if not form.is_valid():
        return _render_form(request, sec, form, obj=obj)
    saved = form.save(commit=False)
    try:
        services.save_item(user, saved, m2m=_m2m(form))
    except NotOwner:
        raise Http404 from None
    return _render_item(request, sec, saved) if _is_htmx(request) else redirect("profiles:profile")


@login_required
@require_POST
def item_delete(request: HttpRequest, section: str, pk: int) -> HttpResponse:
    sec = _section(section)
    user = _user(request)
    obj = get_owned_or_404(sec.model, user, pk=pk)
    try:
        services.delete_item(user, obj)
    except NotOwner:
        raise Http404 from None
    # An empty body with outerHTML swapping removes the row.
    return HttpResponse("") if _is_htmx(request) else redirect("profiles:profile")


@login_required
@require_POST
def item_move(request: HttpRequest, section: str, pk: int) -> HttpResponse:
    sec = _section(section)
    user = _user(request)
    if not sec.ordered:
        raise Http404
    obj = get_owned_or_404(sec.model, user, pk=pk)
    direction = request.POST.get("direction", "")
    if direction not in ("up", "down"):
        return HttpResponse(status=400)
    try:
        services.move_item(user, obj, direction)
    except NotOwner:
        raise Http404 from None
    if not _is_htmx(request):
        return redirect("profiles:profile")
    scope_id = _scope_id(sec, obj)
    manager = sec.model.objects.for_user(user)  # type: ignore[attr-defined]
    items = manager.filter(experience_id=scope_id) if scope_id else manager
    if sec.slug == "experience":
        items = items.prefetch_related("bullets__skills")
    context = {
        "section": sec,
        "items": items.order_by("order", "id"),
        "scope_id": scope_id,
        "bullet_section": SECTIONS["bullet"],
    }
    return render(request, "profiles/_list.html", context)


# --- tracks ----------------------------------------------


@login_required
@require_GET
def tracks(request: HttpRequest) -> HttpResponse:
    return render(
        request, "profiles/tracks.html", {"tracks": Track.objects.for_user(_user(request))}
    )


@login_required
@require_http_methods(["GET", "POST"])
def track_edit(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    user = _user(request)
    track = get_owned_or_404(Track, user, pk=pk) if pk else None
    form = TrackForm(request.POST or None, instance=track)
    if track is not None:
        form.fields["make_default"].initial = track.is_default
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        services.save_track(user, saved, make_default=form.cleaned_data["make_default"])
        messages.success(request, "Track saved.")
        return redirect("profiles:tracks")
    return render(request, "profiles/track_form.html", {"form": form, "track": track})


@login_required
@require_POST
def track_delete(request: HttpRequest, pk: int) -> HttpResponse:
    track = get_owned_or_404(Track, _user(request), pk=pk)
    services.delete_track(_user(request), track)
    messages.success(request, "Track deleted.")
    return redirect("profiles:tracks")


@login_required
@require_POST
def track_default(request: HttpRequest, pk: int) -> HttpResponse:
    track = get_owned_or_404(Track, _user(request), pk=pk)
    services.save_track(_user(request), track, make_default=True)
    messages.success(request, f"“{track.name}” is now your default track.")
    return redirect("profiles:tracks")


# --- CV import ----------------------------------------------

_SELECTION_FIELDS = {
    "exp": "experiences",
    "proj": "projects",
    "edu": "education",
    "cert": "certifications",
    "skill": "skills",
}


def _selection_from(request: HttpRequest) -> services.Selection:
    chosen: dict[str, set[int]] = {}
    for field, attr in _SELECTION_FIELDS.items():
        chosen[attr] = {int(v) for v in request.POST.getlist(field) if v.isdigit()}
    return services.Selection(**chosen)


@login_required
@require_http_methods(["GET", "POST"])
@ratelimit(key="user", rate="10/h", method="POST", block=True)
def import_upload(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    form = CVUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        try:
            cv_import = services.create_cv_import(user, upload.name, upload.read())
        except (InvalidCVFile, UnreadableCV) as exc:
            form.add_error("file", exc.message)
        else:
            return redirect("profiles:import_review", pk=cv_import.pk)
    imports = CVImport.objects.for_user(user)[:5]
    return render(request, "profiles/import_upload.html", {"form": form, "imports": imports})


@login_required
@require_GET
def import_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Polled by the waiting screen; refreshes the whole page once parsing has finished."""
    cv_import = get_owned_or_404(CVImport, _user(request), pk=pk)
    if cv_import.status in (CVImport.Status.PARSED, CVImport.Status.FAILED):
        response = HttpResponse("")
        response["HX-Refresh"] = "true"
        return response
    return render(request, "profiles/_import_waiting.html", {"cv_import": cv_import})


def _review_context(user: User, cv_import: CVImport, basics_form: Any = None) -> dict[str, Any]:
    data = cv_import.parsed_data or {}
    initial = {
        "headline": data.get("headline"),
        "summary": data.get("summary"),
        "phone": data.get("phone"),
        "public_email": data.get("email"),
        "location_text": data.get("location"),
        "years_experience": data.get("years_experience"),
    }
    has_profile = (
        Experience.objects.for_user(user).exists() or Skill.objects.for_user(user).exists()
    )
    return {
        "cv_import": cv_import,
        "data": data,
        "basics_form": basics_form or ReviewBasicsForm(initial=initial),
        "default_mode": "merge" if has_profile else "replace",
        "has_profile": has_profile,
    }


@login_required
@require_GET
def import_review(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    cv_import = get_owned_or_404(CVImport, user, pk=pk)
    context = _review_context(user, cv_import)
    context["next"] = ""
    return render(request, "profiles/import_review.html", context)


@login_required
@require_POST
def import_apply(request: HttpRequest, pk: int) -> HttpResponse:
    user = _user(request)
    cv_import = get_owned_or_404(CVImport, user, pk=pk)
    basics_form = ReviewBasicsForm(request.POST)
    next_url = request.POST.get("next", "")
    if not next_url.startswith("/welcome/"):  # only our own wizard; never an open redirect
        next_url = ""
    if not basics_form.is_valid():
        context = _review_context(user, cv_import, basics_form)
        context["next"] = next_url
        return render(request, "profiles/import_review.html", context, status=400)
    try:
        result = services.apply_import(
            user,
            cv_import,
            mode=request.POST.get("mode", "merge"),
            basics=basics_form.cleaned_data,
            selection=_selection_from(request),
        )
    except ImportStateError as exc:
        messages.error(request, exc.message)
        return redirect("profiles:profile")
    except (ProfileError, ValueError):
        log_event(logger, logging.WARNING, "cv_import_apply_rejected", "Import apply rejected")
        raise Http404 from None
    messages.success(
        request,
        f"Imported {result.experiences} roles, {result.skills} skills, "
        f"{result.education} education entries and {result.projects} projects. "
        "Check the details below and fill in any metrics.",
    )
    return redirect(next_url or reverse("profiles:profile"))


@login_required
@require_GET
def import_file(request: HttpRequest, pk: int) -> FileResponse:
    """Download of the original upload, served only to its owner."""
    cv_import = get_object_or_404(CVImport.objects.for_user(_user(request)), pk=pk)
    return FileResponse(
        cv_import.file.open("rb"),
        as_attachment=True,
        filename=cv_import.original_name or f"cv-{cv_import.pk}",
    )
