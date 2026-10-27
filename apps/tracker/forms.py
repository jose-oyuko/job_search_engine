import datetime
from typing import Any

from django import forms

from apps.notifications.schedule import zone

from .models import Interview, Stage

_DATE = forms.DateInput(attrs={"type": "date"})
_DATETIME = forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"})


class ManualApplicationForm(forms.Form):
    title = forms.CharField(max_length=250, label="Job title")
    company = forms.CharField(max_length=200)
    url = forms.URLField(max_length=500, required=False, label="Link to the job")
    applied_on = forms.DateField(widget=_DATE, label="Date you applied")
    channel = forms.CharField(
        max_length=120, required=False, label="Where you applied",
        help_text="A site, an email address, a referral...",
    )  # fmt: skip
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), required=False, max_length=5000
    )

    def clean_applied_on(self) -> datetime.date:
        value: datetime.date = self.cleaned_data["applied_on"]
        if value > datetime.date.today() + datetime.timedelta(days=1):
            raise forms.ValidationError("The date cannot be in the future.")
        return value


class StageForm(forms.Form):
    stage = forms.ChoiceField(choices=Stage.choices)
    note = forms.CharField(max_length=500, required=False)


class DetailsForm(forms.Form):
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}), required=False, max_length=5000
    )
    channel = forms.CharField(max_length=120, required=False, label="Where you applied")
    outcome_reason = forms.CharField(max_length=300, required=False, label="Outcome or reason")
    salary_offered = forms.CharField(max_length=120, required=False, label="Salary offered")


class KitChoiceForm(forms.Form):
    kit = forms.IntegerField(required=False)


class InterviewForm(forms.ModelForm):
    """`scheduled_at` is typed in the user's own time zone and stored in UTC."""

    scheduled_at = forms.DateTimeField(
        widget=_DATETIME, required=False, input_formats=["%Y-%m-%dT%H:%M"], label="When"
    )

    class Meta:
        model = Interview
        fields = ["scheduled_at", "kind", "notes", "questions_asked", "outcome"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "questions_asked": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args: Any, tz: str = "Africa/Nairobi", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._zone = zone(tz)
        if self.instance.pk and self.instance.scheduled_at:
            local = self.instance.scheduled_at.astimezone(self._zone)
            self.initial["scheduled_at"] = local.strftime("%Y-%m-%dT%H:%M")

    def clean_scheduled_at(self) -> datetime.datetime | None:
        value: datetime.datetime | None = self.cleaned_data.get("scheduled_at")
        if value is None:
            return None
        # Django read the typed time in the server zone; it was meant in the user's own.
        naive = value.replace(tzinfo=None)
        return naive.replace(tzinfo=self._zone).astimezone(datetime.UTC)
