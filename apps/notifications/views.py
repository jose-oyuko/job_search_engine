"""Settings → Notifications: modes, thresholds, channels and Telegram connect/disconnect."""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.accounts.models import User, UserSettings

from . import services
from .exceptions import TelegramUnavailable
from .forms import NotificationSettingsForm
from .gateway import telegram_enabled

logger = logging.getLogger(__name__)


def _user(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]


def telegram_context(user: User, *, new_link: str = "") -> dict[str, object]:
    """What the Telegram panel shows; shared with the onboarding step."""
    link = services.active_link(user)
    return {
        "telegram_enabled": telegram_enabled(),
        "telegram_link": link,
        "connect_url": new_link,
    }


@login_required
@require_http_methods(["GET", "POST"])
def settings_notifications(request: HttpRequest) -> HttpResponse:
    user = _user(request)
    row, _ = UserSettings.objects.get_or_create(user=user)
    form = NotificationSettingsForm(request.POST or None, instance=row)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your notification settings were saved.")
        return redirect("notifications:settings")
    context = {"form": form, **telegram_context(user)}
    return render(request, "notifications/settings.html", context)


@login_required
@require_POST
@ratelimit(key="user", rate="10/h", method="POST", block=True)
def connect_telegram(request: HttpRequest) -> HttpResponse:
    """Make a 15-minute token and show the link that opens the bot with it."""
    user = _user(request)
    row, _ = UserSettings.objects.get_or_create(user=user)
    try:
        token = services.create_link_token(user)
    except TelegramUnavailable as exc:
        messages.error(request, exc.message)
        return redirect("notifications:settings")
    context = {
        "form": NotificationSettingsForm(instance=row),
        **telegram_context(user, new_link=services.link_url(token)),
    }
    return render(request, "notifications/settings.html", context)


@login_required
@require_POST
def disconnect_telegram(request: HttpRequest) -> HttpResponse:
    services.unlink(_user(request))
    messages.success(request, "Telegram was disconnected.")
    return redirect("notifications:settings")
