"""
Admin for the plan catalogue and what customers are on.

Usage is shown read-only and computed live, because the honest answer to "how
much have they sent?" is a count of their messages, not a number somebody could
have typed.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from billing.invoicing import (
    Invoice,
    InvoiceLine,
    InvoiceSequence,
    Payment,
    PaymentWebhookEvent,
)
from billing.models import Plan, Subscription, UsageSnapshot
from billing.usage import summary


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "price_display",
        "interval",
        "max_contacts_display",
        "max_messages_display",
        "is_active",
        "is_public",
        "sort_order",
    )
    list_filter = ("is_active", "is_public", "interval", "featured")
    search_fields = ("name", "slug", "summary")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")

    fieldsets = (
        (None, {"fields": ("name", "slug", "summary", "features")}),
        ("Price", {"fields": ("price", "currency", "interval", "trial_days")}),
        (
            "Limits",
            {
                "fields": ("max_contacts", "max_messages_per_month", "max_team_members"),
                "description": "Leave a limit empty for unlimited. Zero means none at all.",
            },
        ),
        ("Visibility", {"fields": ("is_active", "is_public", "featured", "sort_order")}),
    )

    @admin.display(description="Price", ordering="price")
    def price_display(self, obj: Plan) -> str:
        return f"{obj.currency} {obj.price:,.2f}" if obj.has_price else "On request"

    @admin.display(description="Contacts")
    def max_contacts_display(self, obj: Plan) -> str:
        return "Unlimited" if obj.max_contacts is None else f"{obj.max_contacts:,}"

    @admin.display(description="Messages/mo")
    def max_messages_display(self, obj: Plan) -> str:
        limit = obj.max_messages_per_month
        return "Unlimited" if limit is None else f"{limit:,}"


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "plan",
        "status",
        "current_period_end",
        "cancel_at_period_end",
    )
    list_filter = ("status", "plan", "cancel_at_period_end")
    search_fields = ("organization__name", "organization__slug")
    autocomplete_fields = ("organization", "plan")
    readonly_fields = ("created_at", "updated_at", "usage_display")

    fieldsets = (
        (None, {"fields": ("organization", "plan", "status")}),
        ("Period", {"fields": ("current_period_start", "current_period_end", "trial_end")}),
        ("Cancellation", {"fields": ("cancel_at_period_end", "canceled_at")}),
        ("Usage this period", {"fields": ("usage_display",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Usage")
    def usage_display(self, obj: Subscription) -> str:
        if obj.pk is None:
            return "—"
        rows = []
        for metric, data in summary(obj.organization)["metrics"].items():
            ceiling = "unlimited" if data["limit"] is None else f"{data['limit']:,}"
            rows.append(
                f"<li><strong>{metric.replace('max_', '').replace('_', ' ')}:</strong> "
                f"{data['used']:,} of {ceiling}</li>"
            )
        return format_html("<ul style='margin:0;padding-left:1.1em'>{}</ul>", format_html("".join(rows)))


@admin.register(UsageSnapshot)
class UsageSnapshotAdmin(admin.ModelAdmin):
    """Closed periods are history. Nothing here may be edited."""

    list_display = ("organization", "plan", "period_start", "period_end", "messages_sent")
    list_filter = ("plan", "period_start")
    search_fields = ("organization__name",)
    date_hierarchy = "period_start"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    fields = ("description", "quantity", "unit_amount", "amount")
    readonly_fields = ("amount",)

    def has_change_permission(self, request, obj=None) -> bool:
        """Lines of an issued invoice are history. Void and reissue instead."""
        return not (obj and obj.is_issued)

    def has_delete_permission(self, request, obj=None) -> bool:
        return not (obj and obj.is_issued)


class PaymentInline(admin.TabularInline):
    """Attempts are evidence. Nothing here is editable by hand."""

    model = Payment
    extra = 0
    fields = ("created_at", "provider", "provider_reference", "amount", "status", "error_code")
    readonly_fields = fields
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "organization", "status", "total_display", "due_at", "paid_at")
    list_filter = ("status", "currency", "plan")
    search_fields = ("number", "organization__name")
    date_hierarchy = "created_at"
    autocomplete_fields = ("organization", "plan")
    inlines = [InvoiceLineInline, PaymentInline]
    readonly_fields = (
        "number",
        "amount_paid",
        "issued_at",
        "paid_at",
        "voided_at",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Total", ordering="total")
    def total_display(self, obj: Invoice) -> str:
        due = obj.amount_due
        if due and obj.status == "open":
            return f"{obj.currency} {obj.total:,.2f} ({due:,.2f} due)"
        return f"{obj.currency} {obj.total:,.2f}"

    def get_readonly_fields(self, request, obj=None):
        """An issued invoice is frozen. It is voided and reissued, not edited."""
        if obj and obj.is_issued:
            return [field.name for field in obj._meta.fields]
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None) -> bool:
        """Never. A gap in the sequence is what the sequence exists to avoid."""
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "invoice", "provider", "amount", "status", "error_code")
    list_filter = ("status", "provider")
    search_fields = ("provider_reference", "idempotency_key", "invoice__number")
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PaymentWebhookEvent)
class PaymentWebhookEventAdmin(admin.ModelAdmin):
    """The provider's own words, kept verbatim. Our reading of them is not evidence."""

    list_display = ("created_at", "provider", "event_type", "status", "signature_valid")
    list_filter = ("status", "provider", "signature_valid")
    search_fields = ("event_id", "event_type")
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(admin.ModelAdmin):
    list_display = ("year", "last_number")

    def has_delete_permission(self, request, obj=None) -> bool:
        """Deleting it would restart numbering and reuse numbers already sent."""
        return False
