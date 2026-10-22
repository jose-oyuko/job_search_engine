"""Forms for the profile editor, tracks, CV upload and the onboarding steps."""

import re
from typing import Any, cast

from django import forms
from django.conf import settings
from django.core.validators import URLValidator

from apps.accounts.models import User, UserSettings
from apps.core.countries import country_choices, timezone_choices

from .models import (
    Certification,
    Education,
    EmploymentType,
    Experience,
    ExperienceBullet,
    Profile,
    Project,
    Skill,
    Track,
    WorkType,
)

_SPLIT_RE = re.compile(r"[,\n]")


class TailwindCheckboxSelect(forms.CheckboxSelectMultiple):
    """Checkbox group rendered with our own template (see templates/forms/)."""

    template_name = "forms/checkbox_select.html"
    option_template_name = "forms/checkbox_option.html"


class MonthField(forms.DateField):
    """`<input type="month">`: stores the first day of the month."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("required", False)
        kwargs.setdefault("input_formats", ["%Y-%m", "%Y-%m-%d"])
        kwargs.setdefault("widget", forms.DateInput(format="%Y-%m", attrs={"type": "month"}))
        super().__init__(**kwargs)


class ListField(forms.Field):
    """A list of short strings typed as comma- or line-separated text."""

    widget = forms.Textarea

    def __init__(
        self, *, max_items: int = 30, max_length: int = 100, rows: int = 2, **kwargs: Any
    ) -> None:
        self.max_items = max_items
        self.max_length = max_length
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)
        self.widget.attrs.setdefault("rows", rows)

    def prepare_value(self, value: Any) -> str:
        return ", ".join(value) if isinstance(value, list) else (value or "")

    def to_python(self, value: Any) -> list[str]:
        if not value:
            return []
        items: list[str] = []
        seen: set[str] = set()
        for part in _SPLIT_RE.split(str(value)):
            item = part.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                items.append(item)
        return items

    def validate(self, value: list[str]) -> None:
        super().validate(value)
        if len(value) > self.max_items:
            raise forms.ValidationError(f"Please list at most {self.max_items} items.")
        if any(len(item) > self.max_length for item in value):
            raise forms.ValidationError(f"Each item must be at most {self.max_length} characters.")


class StyledModelForm(forms.ModelForm):
    """Base for forms that may need the acting user to limit related choices."""

    def __init__(self, *args: Any, user: User | None = None, **kwargs: Any) -> None:
        self.user = user
        super().__init__(*args, **kwargs)

    def limit_choices(self, name: str, queryset: Any) -> None:
        """Restrict a related-object field to `queryset` (the acting user's own rows)."""
        field = cast("forms.ModelChoiceField[Any]", self.fields[name])
        field.queryset = queryset


# --- basics ----------------------------------------------


def links_to_text(links: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{link.get('label', '')} | {link.get('url', '')}".strip(" |") for link in links
    )


def parse_links(text: str) -> list[dict[str, str]]:
    """`Label | https://...` per line (a bare URL is allowed). Raises ValidationError."""
    validator = URLValidator(schemes=["http", "https"])
    links: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        label, sep, url = line.rpartition("|")
        url = url.strip()
        label = label.strip() if sep else ""
        try:
            validator(url)
        except forms.ValidationError:
            raise forms.ValidationError(f"“{url[:60]}” is not a valid web address.") from None
        links.append({"label": label[:60], "url": url[:200]})
    if len(links) > 10:
        raise forms.ValidationError("Please add at most 10 links.")
    return links


class ProfileBasicsForm(forms.ModelForm):
    links_text = forms.CharField(
        required=False,
        label="Links",
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": "LinkedIn | https://linkedin.com/in/you"}
        ),
        help_text="One per line as “Label | https://…”.",
    )

    class Meta:
        model = Profile
        fields = [
            "headline",
            "summary",
            "phone",
            "public_email",
            "location_text",
            "years_experience",
        ]
        labels = {
            "public_email": "Contact email",
            "location_text": "Location",
            "years_experience": "Years of experience",
        }
        widgets = {"summary": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["links_text"].initial = links_to_text(self.instance.links or [])

    def clean_links_text(self) -> list[dict[str, str]]:
        return parse_links(self.cleaned_data.get("links_text", ""))

    def save(self, commit: bool = True) -> Profile:
        self.instance.links = self.cleaned_data.get("links_text", [])
        return super().save(commit=commit)


class ReviewBasicsForm(forms.Form):
    """The editable top-level fields on the CV review screen."""

    headline = forms.CharField(max_length=160, required=False)
    summary = forms.CharField(
        max_length=3000, required=False, widget=forms.Textarea(attrs={"rows": 4})
    )
    phone = forms.CharField(max_length=40, required=False)
    public_email = forms.EmailField(required=False, label="Contact email")
    location_text = forms.CharField(max_length=120, required=False, label="Location")
    years_experience = forms.DecimalField(
        required=False,
        min_value=0,
        max_value=99,
        max_digits=4,
        decimal_places=1,
        label="Years of experience",
    )


# --- profile items ----------------------------------------------


class ExperienceForm(StyledModelForm):
    start_date = MonthField(label="Start")
    end_date = MonthField(label="End", help_text="Leave empty if this is your current role.")

    class Meta:
        model = Experience
        fields = [
            "title",
            "company",
            "location",
            "employment_type",
            "start_date",
            "end_date",
            "description",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {"employment_type": "Type"}

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
        return data


class BulletForm(StyledModelForm):
    skills = forms.ModelMultipleChoiceField(
        queryset=Skill.objects.none(), required=False, widget=TailwindCheckboxSelect
    )

    class Meta:
        model = ExperienceBullet
        fields = ["experience", "text", "metrics", "skills"]
        widgets = {"experience": forms.HiddenInput(), "text": forms.Textarea(attrs={"rows": 2})}
        labels = {"text": "What you did", "metrics": "Metrics (optional)"}
        help_texts = {
            "metrics": "Put the real numbers here, e.g. “handled ~30 tickets/day”. "
            "The CV generator may only use numbers you enter.",
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Only the acting user's own rows can be chosen (isolation).
        self.limit_choices("experience", Experience.objects.for_user(self.user))
        self.limit_choices("skills", Skill.objects.for_user(self.user))
        self.fields["skills"].label = "Skills used"


class SkillForm(StyledModelForm):
    class Meta:
        model = Skill
        fields = ["name", "category", "level", "years"]
        labels = {"years": "Years of experience"}

    def clean_name(self) -> str:
        name: str = self.cleaned_data["name"].strip()
        clash = Skill.objects.for_user(self.user).filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("You already have a skill with this name.")
        return name


class ProjectForm(StyledModelForm):
    url = forms.URLField(required=False, assume_scheme="https", label="Link")
    start_date = MonthField(label="Start")
    end_date = MonthField(label="End")
    highlights = ListField(label="Highlights", rows=3, max_items=10, max_length=300)
    technologies = forms.ModelMultipleChoiceField(
        queryset=Skill.objects.none(), required=False, widget=TailwindCheckboxSelect
    )

    class Meta:
        model = Project
        fields = [
            "name",
            "role",
            "url",
            "description",
            "technologies",
            "start_date",
            "end_date",
            "highlights",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.limit_choices("technologies", Skill.objects.for_user(self.user))
        self.fields["highlights"].help_text = "Separate with commas or new lines."


class EducationForm(StyledModelForm):
    start_date = MonthField(label="Start")
    end_date = MonthField(label="End")

    class Meta:
        model = Education
        fields = [
            "institution",
            "qualification",
            "field",
            "start_date",
            "end_date",
            "grade",
            "notes",
        ]


class CertificationForm(StyledModelForm):
    credential_url = forms.URLField(required=False, assume_scheme="https", label="Credential link")
    issued_on = MonthField(label="Issued")
    expires_on = MonthField(label="Expires")

    class Meta:
        model = Certification
        fields = ["name", "issuer", "issued_on", "expires_on", "credential_url", "status"]


# --- tracks ----------------------------------------------


class TrackForm(forms.ModelForm):
    target_titles = ListField(
        label="Job titles you want", help_text="e.g. IT Support, Helpdesk Technician"
    )
    include_keywords = ListField(label="Keywords to look for")
    exclude_keywords = ListField(label="Keywords to avoid")
    work_types = forms.MultipleChoiceField(
        choices=WorkType.choices, required=False, widget=TailwindCheckboxSelect, label="Work types"
    )
    locations = ListField(label="Locations (for on-site roles)")
    employment_types = forms.MultipleChoiceField(
        choices=EmploymentType.choices,
        required=False,
        widget=TailwindCheckboxSelect,
        label="Employment types",
    )
    excluded_companies = ListField(label="Companies to exclude")
    make_default = forms.BooleanField(required=False, label="Use as my default track")

    class Meta:
        model = Track
        fields = [
            "name", "target_titles", "include_keywords", "exclude_keywords", "work_types",
            "locations", "employment_types", "min_pay_amount", "min_pay_currency", "min_pay_period",
            "excluded_companies", "cv_emphasis",
        ]  # fmt: skip
        widgets = {"cv_emphasis": forms.Textarea(attrs={"rows": 3})}
        labels = {
            "min_pay_amount": "Minimum pay",
            "min_pay_currency": "Currency",
            "min_pay_period": "Per",
            "cv_emphasis": "CV emphasis (optional)",
        }
        help_texts = {
            "cv_emphasis": "Guidance for your tailored CVs, e.g. “lead with infrastructure "
            "and support experience”.",
        }

    def clean_min_pay_currency(self) -> str:
        code = self.cleaned_data["min_pay_currency"].strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise forms.ValidationError("Use a 3-letter currency code such as USD or KES.")
        return code


class TrackStartForm(TrackForm):
    """The shorter track form used in the onboarding wizard."""

    class Meta(TrackForm.Meta):
        fields = [
            "name", "target_titles", "include_keywords", "work_types", "employment_types",
            "min_pay_amount", "min_pay_currency", "min_pay_period",
        ]  # fmt: skip

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Declared fields are kept even when absent from Meta.fields, so trim them explicitly.
        for name in list(self.fields):
            if name not in (self._meta.fields or ()):
                del self.fields[name]


# --- CV upload and location ----------------------------------------------


class CVUploadForm(forms.Form):
    file = forms.FileField(label="Your CV (PDF or DOCX, up to 5 MB)")

    def clean_file(self) -> Any:
        upload = self.cleaned_data["file"]
        if upload.size > settings.CV_MAX_UPLOAD_BYTES:
            limit_mb = settings.CV_MAX_UPLOAD_BYTES // (1024 * 1024)
            raise forms.ValidationError(f"That file is too large. The limit is {limit_mb} MB.")
        return upload


class LocationForm(forms.ModelForm):
    country = forms.ChoiceField(choices=country_choices)
    timezone = forms.ChoiceField(choices=timezone_choices)

    class Meta:
        model = UserSettings
        fields = ["country", "timezone"]
