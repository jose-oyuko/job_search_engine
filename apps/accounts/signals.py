"""Signal receivers. Kept thin: the work happens in services."""

from typing import Any

from allauth.account.signals import user_signed_up
from django.dispatch import receiver
from django.http import HttpRequest

from .models import User
from .services import INVITE_SESSION_KEY, complete_signup


@receiver(user_signed_up, dispatch_uid="accounts.complete_signup")
def on_user_signed_up(request: HttpRequest, user: User, **kwargs: Any) -> None:
    code = request.session.pop(INVITE_SESSION_KEY, None)
    complete_signup(user, code)
