from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import InboundEmail

# Metadata only: senders, subjects and bodies are personal and stay out of the admin.
_SHOWN = ("user", "status", "parser", "jobs_found", "received_at", "error", "created_at")


@admin.register(InboundEmail)
class InboundEmailAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "parser", "jobs_found", "received_at")
    list_filter = ("status", "parser")
    fields = _SHOWN
    readonly_fields = _SHOWN

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
