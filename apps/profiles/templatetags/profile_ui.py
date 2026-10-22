"""Template helpers for the profile editor's DOM ids."""

from typing import Any

from django import template

from apps.profiles.sections import Section

register = template.Library()


@register.filter
def list_id(section: Section, scope_id: int | None = None) -> str:
    return section.list_id(scope_id or None)


@register.filter
def new_id(section: Section, scope_id: int | None = None) -> str:
    return section.new_id(scope_id or None)


@register.filter
def item_id(section: Section, pk: int) -> str:
    return section.item_id(pk)


@register.filter
def item_list_id(section: Section, obj: Any) -> str:
    """The id of the list an item sits in (a bullet's list belongs to its experience)."""
    return section.list_id(obj.experience_id if section.parent_field else None)
