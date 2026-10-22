"""allauth adapters that make signup invite-only (spec §15.2).

Existing users are never affected: allauth only consults `is_open_for_signup` when a *new*
account would be created, so logging in (by email or Google) keeps working without an invite.
"""

import logging
from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from django import forms
from django.http import HttpRequest

from apps.core.logging import log_event

from .exceptions import InviteEmailMismatch, InviteError
from .models import Invite
from .services import INVITE_SESSION_KEY, validate_invite

logger = logging.getLogger(__name__)


def invite_from_session(request: HttpRequest) -> Invite | None:
    """The invite whose code the visitor brought via /join/<code>/, if it is still valid."""
    code = request.session.get(INVITE_SESSION_KEY)
    if not code:
        return None
    try:
        return validate_invite(code)
    except InviteError:
        return None


class InviteOnlyAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request: HttpRequest) -> bool:
        is_open = invite_from_session(request) is not None
        if not is_open:
            log_event(
                logger,
                logging.INFO,
                "signup_refused",
                "Signup refused: no valid invite",
                channel="email",
            )
        return is_open

    def clean_email(self, email: str) -> str:
        email = super().clean_email(email)
        request = self.request
        # Only the signup flow is gated; a logged-in user adding a second address is not.
        if request is not None and not request.user.is_authenticated:
            invite = invite_from_session(request)
            if invite and invite.email and invite.email.lower() != email.strip().lower():
                raise forms.ValidationError(InviteEmailMismatch.message)
        return email


class InviteOnlySocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request: HttpRequest, sociallogin: SocialLogin) -> bool:
        invite = invite_from_session(request)
        if invite is None:
            log_event(
                logger,
                logging.INFO,
                "signup_refused",
                "Signup refused: no valid invite",
                channel="google",
            )
            return False
        if invite.email:
            addresses = {a.email.lower() for a in sociallogin.email_addresses}
            if sociallogin.user.email:
                addresses.add(sociallogin.user.email.lower())
            if invite.email.lower() not in addresses:
                log_event(
                    logger,
                    logging.INFO,
                    "signup_refused",
                    "Signup refused: invite email mismatch",
                    channel="google",
                )
                return False
        return True

    def populate_user(
        self, request: HttpRequest, sociallogin: SocialLogin, data: dict[str, Any]
    ) -> Any:
        user = super().populate_user(request, sociallogin, data)
        name = sociallogin.account.extra_data.get("name") or ""
        user.full_name = str(name)[:150]
        return user
