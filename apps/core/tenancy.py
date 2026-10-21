"""Helpers that keep per-user data scoped to the requesting user (spec §16)."""

from typing import Any

from django.db.models import Model, QuerySet
from django.shortcuts import get_object_or_404


def get_owned_or_404(model: type[Model], user: Any, **lookup: Any) -> Any:
    """Fetch one of `user`'s objects; someone else's object is indistinguishable from none."""
    return get_object_or_404(model.objects.for_user(user), **lookup)  # type: ignore[attr-defined]


class OwnedObjectMixin:
    """For class-based views: restricts `get_queryset()` to the requesting user's rows."""

    model: type[Model]
    request: Any

    def get_queryset(self) -> QuerySet[Any]:
        return self.model.objects.for_user(self.request.user)  # type: ignore[attr-defined]
