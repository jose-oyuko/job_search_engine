"""The onboarding wizard (spec §15.3), shown until `UserSettings.onboarding_completed`.

Steps live in the `STEPS` registry. Phases 8 and 10 add the notifications and job-alert steps by
appending to it, so the wizard simply skips over steps that do not exist yet.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.accounts.models import User, UserSettings
from apps.core.logging import log_event

from . import services
from .exceptions import InvalidCVFile, UnreadableCV
from .forms import CVUploadForm, LocationForm, ProfileBasicsForm, TrackStartForm
from .models import CVImport, Track
from .views import _review_context

logger = logging.getLogger(__name__)

DONE = "done"


@dataclass(frozen=True)
class Step:
    slug: str
    title: str
    handler: Callable[[HttpRequest], HttpResponse]


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def step_url(slug: str) -> str:
    return reverse("profiles:onboarding_step", args=[slug])


def next_slug(slug: str) -> str:
    slugs = [s.slug for s in STEPS]
    index = slugs.index(slug)
    return slugs[index + 1] if index + 1 < len(slugs) else DONE


def _render(
    request: HttpRequest, step: Step, template: str, context: dict[str, object]
) -> HttpResponse:
    slugs = [s.slug for s in STEPS]
    context = {
        **context,
        "step": step,
        "step_number": slugs.index(step.slug) + 1,
        "step_total": len(slugs),
        "next_url": step_url(next_slug(step.slug)),
    }
    return render(request, template, context)


# --- step handlers ----------------------------------------------


def _cv_step(request: HttpRequest) -> HttpResponse:
    step = STEP_BY_SLUG["cv"]
    form = CVUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        try:
            services.create_cv_import(_user(request), upload.name, upload.read())
        except (InvalidCVFile, UnreadableCV) as exc:
            form.add_error("file", exc.message)
        else:
            return redirect(step_url(next_slug("cv")))
    return _render(request, step, "profiles/onboarding/cv.html", {"form": form})


def _review_step(request: HttpRequest) -> HttpResponse:
    step = STEP_BY_SLUG["review"]
    user = _user(request)
    latest = (
        CVImport.objects.for_user(user)
        .exclude(status=CVImport.Status.APPLIED)
        .order_by("-created_at")
        .first()
    )
    next_url = step_url(next_slug("review"))
    if latest is not None and latest.status == CVImport.Status.PARSED:
        context = _review_context(user, latest)
        context["next"] = next_url
        return _render(request, step, "profiles/onboarding/review_import.html", context)
    if latest is not None and latest.status in (CVImport.Status.UPLOADED, CVImport.Status.PARSING):
        return _render(
            request, step, "profiles/onboarding/review_waiting.html", {"cv_import": latest}
        )

    # No CV, a failed one, or one already applied: fill in the basics by hand.
    profile = services.get_profile(user)
    form = ProfileBasicsForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save(commit=False)
        services.save_profile(user, profile)
        return redirect(next_url)
    failed = latest if latest is not None and latest.status == CVImport.Status.FAILED else None
    return _render(
        request, step, "profiles/onboarding/review_manual.html", {"form": form, "failed": failed}
    )


def _target_step(request: HttpRequest) -> HttpResponse:
    step = STEP_BY_SLUG["target"]
    user = _user(request)
    track = Track.objects.for_user(user).filter(is_default=True).first()
    form = TrackStartForm(
        request.POST or None, instance=track, initial={} if track else {"name": "My job search"}
    )
    if request.method == "POST" and form.is_valid():
        services.save_track(user, form.save(commit=False), make_default=True)
        return redirect(step_url(next_slug("target")))
    return _render(request, step, "profiles/onboarding/target.html", {"form": form})


def _location_step(request: HttpRequest) -> HttpResponse:
    step = STEP_BY_SLUG["location"]
    settings_row, _ = UserSettings.objects.get_or_create(user=_user(request))
    form = LocationForm(request.POST or None, instance=settings_row)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(step_url(next_slug("location")))
    return _render(request, step, "profiles/onboarding/location.html", {"form": form})


STEPS: list[Step] = [
    Step("cv", "Upload your CV", _cv_step),
    Step("review", "Review your profile", _review_step),
    Step("target", "Choose your target", _target_step),
    Step("location", "Location and timezone", _location_step),
]
STEP_BY_SLUG: dict[str, Step] = {s.slug: s for s in STEPS}


# --- entry points ----------------------------------------------


@login_required
def start(request: HttpRequest) -> HttpResponse:
    return redirect(step_url(STEPS[0].slug))


@login_required
def step(request: HttpRequest, step: str) -> HttpResponse:
    if step == DONE:
        return _finish(request)
    current = STEP_BY_SLUG.get(step)
    if current is None:
        raise Http404
    return current.handler(request)


def _finish(request: HttpRequest) -> HttpResponse:
    from django.contrib import messages

    settings_row, _ = UserSettings.objects.get_or_create(user=_user(request))
    if not settings_row.onboarding_completed:
        settings_row.onboarding_completed = True
        settings_row.save(update_fields=["onboarding_completed", "updated_at"])
        log_event(logger, logging.INFO, "onboarding_completed", "Onboarding completed")
    messages.success(request, "You are all set. Your first matches will appear here soon.")
    return redirect("pages:dashboard")
