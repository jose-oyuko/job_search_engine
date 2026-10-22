"""The profile editor's inline-editable sections, in one registry the generic views read."""

from dataclasses import dataclass

from django.db import models

from . import forms
from .models import Certification, Education, Experience, ExperienceBullet, Project, Skill


@dataclass(frozen=True)
class Section:
    slug: str
    label: str  # singular, for buttons: "Add <label>"
    model: type[models.Model]
    form: type[forms.StyledModelForm]
    ordered: bool  # has up/down reordering
    parent_field: str | None = None  # bullets live inside an experience

    @property
    def item_template(self) -> str:
        return f"profiles/items/_{self.slug}.html"

    def list_id(self, scope_id: int | None = None) -> str:
        return f"list-{self.slug}-{scope_id}" if scope_id else f"list-{self.slug}"

    def new_id(self, scope_id: int | None = None) -> str:
        return f"new-{self.slug}-{scope_id}" if scope_id else f"new-{self.slug}"

    def item_id(self, pk: int) -> str:
        return f"item-{self.slug}-{pk}"


SECTIONS: dict[str, Section] = {
    s.slug: s
    for s in [
        Section("experience", "experience", Experience, forms.ExperienceForm, ordered=True),
        Section(
            "bullet",
            "bullet point",
            ExperienceBullet,
            forms.BulletForm,
            ordered=True,
            parent_field="experience",
        ),  # fmt: skip
        Section("skill", "skill", Skill, forms.SkillForm, ordered=False),
        Section("project", "project", Project, forms.ProjectForm, ordered=True),
        Section("education", "education", Education, forms.EducationForm, ordered=True),
        Section(
            "certification", "certification", Certification, forms.CertificationForm, ordered=True
        ),
    ]
}
