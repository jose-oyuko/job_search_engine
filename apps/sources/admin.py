from typing import Any

from django.contrib import admin, messages
from django.db.models import Count, QuerySet
from django.http import HttpRequest
from django.utils.html import format_html

from apps.core.tasks import enqueue

from .models import Source, SourceRun
from .tasks import run_source_task

_HEALTH_COLOURS = {"ok": "#15803d", "degraded": "#b45309", "failing": "#b91c1c"}


class SourceRunInline(admin.TabularInline):
    """The ten most recent runs, read-only."""

    model = SourceRun
    extra = 0
    can_delete = False
    fields = ("started_at", "trigger", "success", "ids_seen", "new_jobs", "updated_jobs", "errors")
    readonly_fields = fields
    verbose_name_plural = "Recent runs (latest 10)"

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_queryset(self, request: HttpRequest) -> QuerySet[SourceRun]:
        queryset = super().get_queryset(request)
        latest = queryset.order_by("-started_at", "-id").values_list("pk", flat=True)[:10]
        return queryset.filter(pk__in=list(latest)).order_by("-started_at", "-id")


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name", "display_name", "kind", "enabled", "health_badge", "last_run_at",
        "last_success_at", "consecutive_failures", "job_count",
    )  # fmt: skip
    list_filter = ("kind", "enabled", "health")
    search_fields = ("name", "display_name")
    readonly_fields = ("health", "last_run_at", "last_success_at", "consecutive_failures")
    inlines = (SourceRunInline,)
    actions = ("run_now",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Source]:
        return super().get_queryset(request).annotate(_jobs=Count("jobs"))

    @admin.display(description="Health", ordering="health")
    def health_badge(self, obj: Source) -> str:
        return format_html(
            '<strong style="color:{}">{}</strong>',
            _HEALTH_COLOURS.get(obj.health, "#374151"),
            obj.get_health_display(),
        )

    @admin.display(description="Jobs", ordering="_jobs")
    def job_count(self, obj: Source) -> int:
        return int(getattr(obj, "_jobs", 0))

    @admin.action(description="Run now (queued to the worker)", permissions=["change"])
    def run_now(self, request: HttpRequest, queryset: QuerySet[Source]) -> None:
        for source in queryset:
            enqueue(run_source_task, source.pk, SourceRun.Trigger.ADMIN)
        self.message_user(
            request,
            f"Queued {queryset.count()} run(s). Make sure run_huey is running; "
            "results appear under Recent runs.",
            messages.SUCCESS,
        )


@admin.register(SourceRun)
class SourceRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "started_at",
        "trigger",
        "success",
        "ids_seen",
        "new_jobs",
        "updated_jobs",
    )
    list_filter = ("source", "trigger", "success")
    readonly_fields = [f.name for f in SourceRun._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
