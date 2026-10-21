"""Abstract base models shared by every app (spec §5.1)."""

from typing import Any, Self

from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UserOwnedQuerySet(models.QuerySet[Any]):
    def for_user(self, user: Any) -> Self:
        """Only rows owned by `user`; anonymous or missing users get nothing."""
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(user=user)


class UserOwnedModel(models.Model):
    """Base for every per-user model. Query through `Model.objects.for_user(request.user)`."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, db_index=True, related_name="+"
    )

    objects = UserOwnedQuerySet.as_manager()

    class Meta:
        abstract = True
