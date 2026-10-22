"""Extra fields on the allauth signup forms (email and Google): a name and the consent box."""

from typing import Any

from django import forms
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html

from .models import User


class SignupExtrasForm(forms.Form):
    """Plugged in through ACCOUNT_SIGNUP_FORM_CLASS; allauth merges it into both signup forms."""

    full_name = forms.CharField(max_length=150, required=False, label="Full name")
    accept_terms = forms.BooleanField(
        required=True,
        error_messages={"required": "You need to accept the terms and privacy policy to join."},
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        link_class = "underline"
        self.fields["accept_terms"].label = format_html(
            'I agree to the <a href="{}" target="_blank" rel="noopener" class="{}">Terms of use</a>'
            ' and the <a href="{}" target="_blank" rel="noopener" class="{}">Privacy policy</a>.',
            reverse("pages:terms"),
            link_class,
            reverse("pages:privacy"),
            link_class,
        )

    def signup(self, request: HttpRequest, user: User) -> None:
        # The consent rows themselves are written by services.complete_signup; reaching this
        # point means the checkbox was ticked, because the form validated.
        name = self.cleaned_data.get("full_name", "").strip()
        if name:
            user.full_name = name
            user.save(update_fields=["full_name"])
