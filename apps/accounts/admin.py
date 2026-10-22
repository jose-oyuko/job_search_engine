from typing import Any

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from apps.llm.models import UsageQuota

from .models import ConsentRecord, Invite, User, UserSettings, WaitlistEntry
from .services import (
    approve_waitlist_entry,
    generate_invites,
    invite_url,
    send_invite_email,
)


class UserSettingsInline(admin.StackedInline):
    model = UserSettings
    can_delete = False


class UsageQuotaInline(admin.StackedInline):
    model = UsageQuota
    can_delete = False


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = ("email", "full_name", "is_staff", "is_active", "date_joined")
    search_fields = ("email", "full_name")
    inlines = (UserSettingsInline, UsageQuotaInline)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("full_name",)}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)

    def get_inline_instances(self, request: HttpRequest, obj: User | None = None) -> list[Any]:
        # Settings and quota are created together with the user, never through the add form.
        return super().get_inline_instances(request, obj) if obj is not None else []

    def save_model(self, request: HttpRequest, obj: User, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        if not change:
            UserSettings.objects.get_or_create(user=obj)
            UsageQuota.objects.get_or_create(user=obj)


class GenerateInvitesForm(forms.Form):
    count = forms.IntegerField(min_value=1, max_value=100, initial=5)
    note = forms.CharField(max_length=200, required=False)
    expires_in_days = forms.IntegerField(min_value=1, max_value=365, initial=14)


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "note", "status", "created_by", "expires_at", "used_by")
    list_filter = ("created_at",)
    search_fields = ("email", "note")
    fields = ("email", "note", "expires_at", "invite_link", "created_by", "used_by", "used_at")
    readonly_fields = ("invite_link", "created_by", "used_by", "used_at")
    actions = ("copy_links", "email_invites")
    change_list_template = "admin/accounts/invite/change_list.html"

    @admin.display(description="Status")
    def status(self, obj: Invite) -> str:
        if obj.is_used:
            return "used"
        return "expired" if obj.is_expired else "active"

    @admin.display(description="Invite link")
    def invite_link(self, obj: Invite) -> str:
        if obj.pk is None:
            return "Saved after you click Save."
        return format_html(
            '<input type="text" readonly size="70" value="{}" onfocus="this.select()">',
            invite_url(obj),
        )

    def save_model(self, request: HttpRequest, obj: Invite, form: Any, change: bool) -> None:
        if not change:
            obj.created_by = request.user  # type: ignore[assignment]
        obj.email = obj.email.strip().lower()
        super().save_model(request, obj, form, change)

    @admin.action(description="Show invite links (to copy)")
    def copy_links(self, request: HttpRequest, queryset: QuerySet[Invite]) -> None:
        for invite in queryset:
            if invite.is_used or invite.is_expired:
                self.message_user(request, f"Invite #{invite.pk} is not usable.", messages.WARNING)
            else:
                self.message_user(request, f"Invite #{invite.pk}: {invite_url(invite)}")

    @admin.action(description="Email invite to its locked address")
    def email_invites(self, request: HttpRequest, queryset: QuerySet[Invite]) -> None:
        for invite in queryset:
            if not invite.email:
                self.message_user(
                    request,
                    f"Invite #{invite.pk} has no email address; use 'Show invite links'.",
                    messages.WARNING,
                )
            elif send_invite_email(invite, invite.email):
                self.message_user(request, f"Invite #{invite.pk} emailed.")
            else:
                self.message_user(
                    request, f"Invite #{invite.pk} could not be sent.", messages.ERROR
                )

    def get_urls(self) -> list[Any]:
        custom = [
            path(
                "generate/",
                self.admin_site.admin_view(self.generate_view),
                name="accounts_invite_generate",
            )
        ]
        return custom + super().get_urls()

    def generate_view(self, request: HttpRequest) -> HttpResponse:
        if not self.has_add_permission(request):
            return redirect("admin:accounts_invite_changelist")
        form = GenerateInvitesForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            invites = generate_invites(
                created_by=request.user,  # type: ignore[arg-type]
                count=form.cleaned_data["count"],
                note=form.cleaned_data["note"],
                expires_in_days=form.cleaned_data["expires_in_days"],
            )
            return render(
                request,
                "admin/accounts/invite/generated.html",
                {
                    **self.admin_site.each_context(request),
                    "title": "Invites generated",
                    "links": [invite_url(i) for i in invites],
                    "changelist_url": reverse("admin:accounts_invite_changelist"),
                },
            )
        return render(
            request,
            "admin/accounts/invite/generate.html",
            {**self.admin_site.each_context(request), "title": "Generate invites", "form": form},
        )


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "email", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "email")
    readonly_fields = ("invite",)
    actions = ("approve", "reject")

    @admin.action(description="Approve: create an invite and email it", permissions=["change"])
    def approve(self, request: HttpRequest, queryset: QuerySet[WaitlistEntry]) -> None:
        for entry in queryset.filter(status__in=["pending", "rejected"]):
            _, emailed = approve_waitlist_entry(entry, request.user)  # type: ignore[arg-type]
            if emailed:
                self.message_user(request, f"Invite emailed to {entry.email}.")
            else:
                self.message_user(
                    request,
                    f"Invite created for {entry.email} but the email failed; "
                    "use the Invites page to resend.",
                    messages.WARNING,
                )

    @admin.action(description="Reject", permissions=["change"])
    def reject(self, request: HttpRequest, queryset: QuerySet[WaitlistEntry]) -> None:
        updated = queryset.filter(status=WaitlistEntry.Status.PENDING).update(
            status=WaitlistEntry.Status.REJECTED
        )
        self.message_user(request, f"{updated} request(s) rejected.")


@admin.register(UserSettings)
class UserSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "country", "timezone", "notify_mode", "onboarding_completed")
    search_fields = ("user__email",)
    raw_id_fields = ("user",)


@admin.register(UsageQuota)
class UsageQuotaAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "monthly_scores",
        "monthly_kits",
        "monthly_manual_extractions",
        "monthly_cost_usd_cap",
    )
    search_fields = ("user__email",)
    raw_id_fields = ("user",)


@admin.register(ConsentRecord)
class ConsentRecordAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "version", "granted", "at")
    list_filter = ("kind", "granted")
    raw_id_fields = ("user",)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False  # consent history is append-only
