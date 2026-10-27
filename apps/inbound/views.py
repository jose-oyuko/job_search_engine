"""Settings → Job alerts: the user's forwarding address, how to use it and what arrived."""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User

from . import services
from .mailbox import mailbox_enabled


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def alerts_context(user: User) -> dict[str, object]:
    """Shared by the settings page and onboarding."""
    address = services.ensure_address(user)
    return {
        "inbound_address": address.address,
        "ingestion_enabled": mailbox_enabled(),
        "confirmation": services.pending_confirmation(user),
    }


@login_required
@require_GET
def job_alerts(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    context = {**alerts_context(user), "emails": services.recent_emails(user)}
    return render(request, "inbound/settings.html", context)


@login_required
@require_POST
def dismiss_confirmation(request: HttpRequest, pk: int) -> HttpResponse:
    services.dismiss_confirmation(_user(request), pk)
    return _after(request)


def _after(request: HttpRequest) -> HttpResponse:
    return redirect("pages:dashboard" if request.POST.get("next") == "feed" else "inbound:settings")
