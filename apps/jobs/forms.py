from django import forms

from .models import JobReport


class UrlForm(forms.Form):
    url = forms.CharField(
        max_length=2000,
        label="Link to the job",
        widget=forms.TextInput(
            attrs={
                "placeholder": "https://example.com/careers/support-engineer",
                "inputmode": "url",
            }
        ),
    )


class TextForm(forms.Form):
    text = forms.CharField(
        label="Job description",
        widget=forms.Textarea(attrs={"rows": 10, "placeholder": "Paste the full job posting here"}),
        max_length=40000,  # the service enforces the real limits and explains them
    )
    title = forms.CharField(max_length=250, required=False, label="Job title")
    company = forms.CharField(max_length=200, required=False, label="Company")
    url = forms.CharField(
        max_length=2000, required=False, label="Link to the original posting",
        widget=forms.TextInput(attrs={"inputmode": "url"}),
    )  # fmt: skip


class ReportForm(forms.Form):
    reason = forms.ChoiceField(choices=JobReport.Reason.choices, label="What is wrong?")
    note = forms.CharField(max_length=500, required=False, label="Details (optional)")
