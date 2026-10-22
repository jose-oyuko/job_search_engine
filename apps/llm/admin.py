from typing import Any

from django.contrib import admin
from django.http import HttpRequest, HttpResponse

from .models import LLMCache, LLMCall
from .services import spend_summary


@admin.register(LLMCall)
class LLMCallAdmin(admin.ModelAdmin):
    """Read-only call log with a spend summary on top (staff-only cross-user view)."""

    list_display = (
        "id", "created_at", "user", "task", "model", "input_tokens", "output_tokens",
        "cost_usd", "latency_ms", "success", "cache_hit",
    )  # fmt: skip
    list_filter = ("task", "success", "cache_hit", "provider", "created_at")
    search_fields = ("user__email", "model")
    date_hierarchy = "created_at"
    change_list_template = "admin/llm/llmcall/change_list.html"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        context = {**(extra_context or {}), "spend": spend_summary()}
        return super().changelist_view(request, extra_context=context)


@admin.register(LLMCache)
class LLMCacheAdmin(admin.ModelAdmin):
    list_display = ("id", "key", "prompt_version", "created_at")
    search_fields = ("key",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
