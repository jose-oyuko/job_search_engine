from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import Notification, TelegramLink

# Metadata only: chat IDs and message content are personal and stay out of the admin.


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "kind", "channel", "status", "attempts", "sent_at", "created_at")
    list_filter = ("kind", "channel", "status")
    fields = (
        "user", "kind", "channel", "status", "attempts", "deliver_after", "sent_at", "error",
        "created_at",
    )  # fmt: skip
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(TelegramLink)
class TelegramLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "active", "linked_at")
    fields = ("user", "active", "linked_at")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
