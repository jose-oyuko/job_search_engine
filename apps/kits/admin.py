from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import Kit

# Metadata only: the documents and their text are personal and stay out of the admin.
_SHOWN = (
    "user", "job", "match", "track", "version", "status", "progress", "error", "auto_generated",
    "user_edited", "template", "profile_version", "created_at", "updated_at",
)  # fmt: skip


@admin.register(Kit)
class KitAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "job", "version", "status", "auto_generated", "created_at")
    list_filter = ("status", "auto_generated", "user_edited")
    fields = _SHOWN
    readonly_fields = _SHOWN

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
