import re

from django import forms
from django.http import QueryDict

from .services import MAX_INSTRUCTION_CHARS, KitEdits

_BULLET_KEY = re.compile(r"^(exp|prj)-(\d{1,2})-(\d{1,2})$")
MAX_TEXT = 6000


class GenerateForm(forms.Form):
    instruction = forms.CharField(
        max_length=MAX_INSTRUCTION_CHARS,
        required=False,
        label="Instruction (optional)",
        help_text="For example: shorter, more technical, emphasise IT support.",
    )


def parse_edits(post: QueryDict) -> KitEdits:
    """Read the edit form: headline, summary, cover letter and `exp-i-j` / `prj-i-j` bullets.
    Anything that is not one of those fields is ignored."""
    get = post.get
    bullets: dict[tuple[str, int, int], str] = {}
    for key in list(post.keys()):
        found = _BULLET_KEY.match(key)
        if found:
            section = "exp" if found.group(1) == "exp" else "proj"
            bullets[(section, int(found.group(2)), int(found.group(3)))] = str(get(key))[:600]
    return KitEdits(
        headline=_text(get("headline")),
        summary=_text(get("summary")),
        cover_letter=_text(get("cover_letter")),
        bullets=bullets,
    )


def _text(value: object) -> str | None:
    return None if value is None else str(value)[:MAX_TEXT]
