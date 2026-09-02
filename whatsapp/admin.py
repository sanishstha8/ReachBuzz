from django.contrib import admin

from whatsapp.models import MessageTemplate, WebhookEvent


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


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    """
    Read-only: an event is evidence of what Meta sent, and editing evidence
    defeats the point of keeping it. Reprocessing is the supported action.
    """

    list_display = ("created_at", "status", "status_count", "message_count", "processed_at")
    list_filter = ("status", "signature_valid")
    search_fields = ("error_message",)
    readonly_fields = (
        "payload",
        "signature_valid",
        "status",
        "status_count",
        "message_count",
        "processed_at",
        "error_message",
        "created_at",
        "updated_at",
    )
    actions = ("reprocess",)

    def has_add_permission(self, request) -> bool:
        return False

    @admin.action(description="Reprocess the selected events")
    def reprocess(self, request, queryset) -> None:
        """
        Re-run processing for events that failed on our side.

        Safe to use on anything: applying a status update twice is a no-op, so
        a needless reprocess costs a query rather than a duplicate message.
        """
        from whatsapp.tasks import process_webhook_event_task

        for event in queryset:
            process_webhook_event_task.delay(str(event.pk))
        self.message_user(request, f"Queued {queryset.count()} event(s) for reprocessing.")
