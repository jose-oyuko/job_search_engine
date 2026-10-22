"""Public pages, the waitlist form and the error handlers."""

import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseServerError
from django.shortcuts import redirect, render
from django.template.loader import get_template
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited

from apps.accounts.services import submit_waitlist
from apps.core.logging import log_event

from .forms import WaitlistForm

logger = logging.getLogger(__name__)


def landing(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("pages:dashboard")
    return render(request, "pages/landing.html", {"form": WaitlistForm()})


@require_POST
@ratelimit(key="ip", rate="5/h", method="POST", block=False)
def waitlist(request: HttpRequest) -> HttpResponse:
    if getattr(request, "limited", False):
        log_event(logger, logging.WARNING, "rate_limited", "Rate limit hit", scope="waitlist")
        return render(request, "pages/rate_limited.html", status=429)

    form = WaitlistForm(request.POST)
    if not form.is_valid():
        return render(request, "pages/landing.html", {"form": form}, status=400)
    if form.cleaned_data["website"]:
        # Honeypot tripped: look like success, store nothing.
        log_event(logger, logging.INFO, "waitlist_honeypot", "Waitlist honeypot tripped")
    else:
        submit_waitlist(
            name=form.cleaned_data["name"],
            email=form.cleaned_data["email"],
            reason=form.cleaned_data["reason"],
        )
    return render(request, "pages/waitlist_thanks.html")


@require_GET
def privacy(request: HttpRequest) -> HttpResponse:
    return render(request, "pages/privacy.html")


@require_GET
def terms(request: HttpRequest) -> HttpResponse:
    return render(request, "pages/terms.html")


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Placeholder shell; the job feed replaces it in Phase 6."""
    return render(request, "pages/dashboard.html")


def permission_denied(
    request: HttpRequest, exception: Exception | None = None, template_name: str = ""
) -> HttpResponse:
    """403 handler. django-ratelimit raises `Ratelimited` (a PermissionDenied), which should read
    as 'slow down' rather than 'forbidden'."""
    if isinstance(exception, Ratelimited):
        log_event(logger, logging.WARNING, "rate_limited", "Rate limit hit", scope="view")
        return render(request, "pages/rate_limited.html", status=429)
    return render(request, "403.html", status=403)


def not_found(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    return render(request, "404.html", status=404)


def server_error(request: HttpRequest) -> HttpResponse:
    # Rendered without a request: context processors or the database may be what failed, and
    # 500.html is standalone so it does not need them.
    return HttpResponseServerError(get_template("500.html").render())
