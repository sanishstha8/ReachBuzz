from django.contrib import admin

from whatsapp.models import MessageTemplate


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "language", "category", "source", "status", "variable_count", "updated_at")
    list_filter = ("source", "status", "category", "language")
    search_fields = ("name", "body_text")
    readonly_fields = ("variables", "provider_template_id", "synced_at", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("name", "language", "category")}),
        ("Content", {"fields": ("header_text", "body_text", "footer_text", "variables", "example_values")}),
        (
            "Approval",
            {
                "fields": ("source", "status", "provider_template_id", "synced_at", "rejection_reason"),
                "description": (
                    "This mirrors Meta's approval decision. Marking a template approved here "
                    "does not make it approved with Meta, and campaigns using a local template "
                    "are refused under the live provider."
                ),
            },
        ),
    )
