"""
Admin for the tenant itself.

Missing until now — Stage 1 added the models and the isolation but never
registered them, which left no way to look at a customer without a shell. The
platform admin dashboard is a later stage; this is the interim.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from organizations.models import Organization, OrganizationMember


class OrganizationMemberInline(admin.TabularInline):
    model = OrganizationMember
    extra = 0
    autocomplete_fields = ("user",)
    fields = ("user", "role", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner", "status", "plan_display", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("name", "slug", "owner__email")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("owner",)
    inlines = [OrganizationMemberInline]
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Plan")
    def plan_display(self, obj: Organization) -> str:
        subscription = getattr(obj, "subscription", None)
        if subscription is None:
            return format_html('<span style="color:#b91c1c">No subscription</span>')
        url = reverse("admin:billing_subscription_change", args=[subscription.pk])
        return format_html(
            '<a href="{}">{}</a> <small>({})</small>',
            url,
            subscription.plan.name,
            subscription.get_status_display(),
        )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("owner", "subscription__plan")


@admin.register(OrganizationMember)
class OrganizationMemberAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "created_at")
    list_filter = ("role", "organization")
    search_fields = ("user__email", "organization__name")
    autocomplete_fields = ("user", "organization")
