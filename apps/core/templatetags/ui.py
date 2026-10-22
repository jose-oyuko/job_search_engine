"""Form-rendering helpers so Django and allauth forms share the app's Tailwind styling."""

from django import forms, template
from django.forms.boundfield import BoundField
from django.utils.safestring import SafeString

register = template.Library()

_INPUT = "input"
_CHECKBOX = "mt-1 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-600"


@register.filter
def is_checkbox(field: BoundField) -> bool:
    """A single checkbox, which is laid out with its label beside it."""
    return isinstance(field.field.widget, forms.CheckboxInput)


@register.filter
def is_checkbox_group(field: BoundField) -> bool:
    return isinstance(field.field.widget, forms.CheckboxSelectMultiple)


@register.filter
def styled(field: BoundField) -> SafeString:
    """Render the widget with our CSS classes and accessible error wiring."""
    boxed = is_checkbox(field) or is_checkbox_group(field)
    attrs: dict[str, str | bool] = {"class": _CHECKBOX if boxed else _INPUT}
    if field.errors:
        attrs["aria-invalid"] = True
    return field.as_widget(attrs=attrs)
