"""Thin views: invite landing page and the settings → account page."""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET
from django_ratelimit.decorators import ratelimit

from apps.core.logging import log_event

from .exceptions import InviteError
from .services import INVITE_SESSION_KEY, validate_invite

logger = logging.getLogger(__name__)


@require_GET
@ratelimit(key="ip", rate="10/m", method="GET", block=True)
def join(request: HttpRequest, code: str) -> HttpResponse:
    """Validate the invite, remember it in the session, and offer the two ways to sign up."""
    try:
        invite = validate_invite(code)
    except InviteError as exc:
        # The code is the credential, so it is never logged; only the reason is.
        log_event(logger, logging.INFO, "invite_rejected", "Invite rejected", reason=exc.reason)
        request.session.pop(INVITE_SESSION_KEY, None)
        return render(request, "accounts/join_invalid.html", {"error": exc}, status=404)

    if request.user.is_authenticated:
        messages.info(request, "You already have an account, so this invite is not needed.")
        return redirect("pages:dashboard")

    request.session[INVITE_SESSION_KEY] = invite.code
    return render(request, "accounts/join.html", {"locked_email": invite.email})


@login_required
def account_settings(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/settings_account.html")
