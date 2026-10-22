import json
from typing import Any

from django import forms
from django.contrib import admin
from django.db import models
from django.utils.html import format_html

from .models import Job, JobReport


def _pretty(value: Any) -> str:
    return format_html(
        '<pre style="white-space:pre-wrap;max-width:60rem">{}</pre>',
        json.dumps(value, indent=2, ensure_ascii=False, default=str),
    )


class HttpsURLField(forms.URLField):
    """Bare addresses typed into the admin mean https (the Django 6 default)."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("assume_scheme", "https")
        super().__init__(**kwargs)


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    formfield_overrides = {models.URLField: {"form_class": HttpsURLField}}
    list_display = (
        "title", "company", "source", "origin", "status", "work_type", "pay_text", "is_global",
        "first_seen_at", "duplicate_of",
    )  # fmt: skip
    list_filter = (
        "source", "status", "origin", "visibility", "work_type", "is_global", "eligibility_known",
        "pay_period", ("duplicate_of", admin.EmptyFieldListFilter),
    )  # fmt: skip
    search_fields = ("title", "company", "external_id", "dedupe_key")
    date_hierarchy = "first_seen_at"
    raw_id_fields = ("owner", "duplicate_of")
    readonly_fields = (
        "raw_json",
        "extracted_json",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )
    exclude = ("raw", "extracted")
    fieldsets = (
        (
            "Job",
            {
                "fields": (
                    "title", "company", "company_platform", "url", "apply_url",
                    "alternate_urls",
                )
            },
        ),
        (
            "Source",
            {"fields": ("source", "external_id", "origin", "visibility", "owner", "status")},
        ),
        ("Pay", {"fields": ("pay_text", "pay_min", "pay_max", "pay_currency", "pay_period")}),
        (
            "Eligibility",
            {
                "fields": (
                    "locations_raw", "eligible_countries", "eligible_regions", "is_global",
                    "eligibility_known", "work_type", "employment_type",
                )
            },
        ),
        ("Text", {"fields": ("description_text", "description_html"), "classes": ("collapse",)}),
        (
            "Lifecycle",
            {
                "fields": (
                    "posted_at", "first_seen_at", "last_seen_at", "dedupe_key", "duplicate_of",
                    "scam_flags",
                )
            },
        ),
        ("Extraction", {"fields": ("extracted_at", "extracted_json")}),
        ("Raw payload", {"fields": ("raw_json",), "classes": ("collapse",)}),
    )  # fmt: skip

    @admin.display(description="Raw payload")
    def raw_json(self, obj: Job) -> str:
        return _pretty(obj.raw)

    @admin.display(description="Extracted")
    def extracted_json(self, obj: Job) -> str:
        return _pretty(obj.extracted) if obj.extracted else "Not extracted yet."


@admin.register(JobReport)
class JobReportAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "user", "reason", "created_at")
    list_filter = ("reason",)
    raw_id_fields = ("job", "user")
