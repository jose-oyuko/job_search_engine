from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import Match


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    """Read-only view across users for staff debugging."""

    list_display = ("id", "user", "job", "score", "verdict", "status", "rule_pass", "created_at")
    list_filter = ("status", "verdict", "rule_pass")
    search_fields = ("user__email", "job__title", "job__company")
    raw_id_fields = ("user", "job", "track")
    readonly_fields = [f.name for f in Match._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
