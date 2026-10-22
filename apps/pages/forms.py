from django import forms


class WaitlistForm(forms.Form):
    name = forms.CharField(max_length=150, label="Your name")
    email = forms.EmailField(label="Email address")
    reason = forms.CharField(
        max_length=1000,
        required=False,
        label="What are you looking for?",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    # Honeypot: real visitors never see or fill this; naive bots do.
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={"tabindex": "-1"}))
