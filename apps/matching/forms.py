from django import forms

from apps.profiles.models import Track

from .models import Match


class FeedFilterForm(forms.Form):
    """Feed filters from the query string. Bad values are ignored, not shown as errors."""

    track = forms.IntegerField(required=False)
    verdict = forms.ChoiceField(choices=[("", "Any")] + list(Match.Verdict.choices), required=False)
    status = forms.ChoiceField(
        choices=[("", "Active")]
        + [(s.value, s.label) for s in Match.Status if s.value != Match.Status.NOTIFIED],
        required=False,
    )
    work_type = forms.ChoiceField(
        choices=[("", "Any"), ("remote", "Remote"), ("onsite", "On-site"), ("hybrid", "Hybrid")],
        required=False,
    )
    q = forms.CharField(max_length=100, required=False, label="Company or title")
    eligible = forms.BooleanField(required=False, label="Open to my country only")
    new = forms.BooleanField(required=False, label="New only")

    def __init__(self, *args: object, user: object = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        tracks = Track.objects.for_user(user) if user is not None else Track.objects.none()
        self.fields["track"] = forms.TypedChoiceField(
            choices=[("", "All tracks")] + [(t.pk, t.name) for t in tracks],
            coerce=int,
            empty_value=None,
            required=False,
        )


class DismissForm(forms.Form):
    reason = forms.ChoiceField(choices=Match.DismissReason.choices, required=False)
