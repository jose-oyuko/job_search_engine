"""Forms for Settings → Notifications and the onboarding notifications step."""

from typing import Any

from django import forms

from apps.accounts.models import UserSettings

from .gateway import telegram_enabled
from .services import active_link

_TIME = forms.TimeInput(format="%H:%M", attrs={"type": "time"})


class NotificationSettingsForm(forms.ModelForm):
    digest_time = forms.TimeField(
        widget=_TIME, input_formats=["%H:%M", "%H:%M:%S"], label="Digest time",
        help_text="Your local time. Used for the daily digest.",
    )  # fmt: skip
    quiet_hours_start = forms.TimeField(
        widget=_TIME, input_formats=["%H:%M", "%H:%M:%S"], required=False, label="Quiet hours start"
    )
    quiet_hours_end = forms.TimeField(
        widget=_TIME, input_formats=["%H:%M", "%H:%M:%S"], required=False, label="Quiet hours end",
        help_text="Instant and hourly messages wait until quiet hours end.",
    )  # fmt: skip
    instant_score_threshold = forms.IntegerField(
        min_value=0, max_value=100, label="Send instant alerts from this score"
    )
    auto_kit_threshold = forms.IntegerField(
        min_value=0, max_value=100, label="Prepare a kit automatically from this score"
    )

    class Meta:
        model = UserSettings
        fields = [
            "notify_mode",
            "digest_time",
            "quiet_hours_start",
            "quiet_hours_end",
            "instant_score_threshold",
            "auto_kit_threshold",
            "notify_telegram",
            "notify_email",
            "send_documents_telegram",
        ]
        labels = {
            "notify_mode": "How often",
            "notify_telegram": "Send alerts on Telegram",
            "notify_email": "Send alerts by email",
            "send_documents_telegram": "Send my CV and cover letter on Telegram when ready",
        }

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        start, end = data.get("quiet_hours_start"), data.get("quiet_hours_end")
        if (start is None) != (end is None):
            self.add_error(
                "quiet_hours_end", "Set both the start and the end, or leave both empty."
            )
        if data.get("notify_telegram") and self.instance.pk:
            if not telegram_enabled():
                self.add_error("notify_telegram", "Telegram is not set up on this server yet.")
            elif active_link(self.instance.user) is None:
                self.add_error("notify_telegram", "Connect Telegram first, then turn this on.")
        return data


class OnboardingNotificationsForm(forms.ModelForm):
    digest_time = forms.TimeField(
        widget=_TIME, input_formats=["%H:%M", "%H:%M:%S"], label="Digest time",
        help_text="Your local time. Matches below your instant score arrive in one daily digest.",
    )  # fmt: skip

    class Meta:
        model = UserSettings
        fields = ["notify_mode", "digest_time", "notify_email"]
        labels = {"notify_mode": "How often", "notify_email": "Send alerts by email"}
