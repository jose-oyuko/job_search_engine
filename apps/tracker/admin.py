from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import Application

# Metadata only: notes, channel and outcome text are personal and stay out of the admin.
_SHOWN = (
    "user",
    "job",
    "stage",
    "applied_at",
    "last_stage_change_at",
    "next_followup_at",
    "created_at",
)


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "job", "stage", "applied_at", "last_stage_change_at")
    list_filter = ("stage",)
    fields = _SHOWN
    readonly_fields = _SHOWN

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
