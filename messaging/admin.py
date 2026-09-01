from django.contrib import admin

from messaging.models import Message, MessageStatusEvent


class MessageStatusEventInline(admin.TabularInline):
    model = MessageStatusEvent
    extra = 0
    can_delete = False
    readonly_fields = ("status", "source", "provider_timestamp", "error_code", "error_message", "created_at")

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    """Message state belongs to the worker and the provider: read-only here."""

    list_display = ("to_phone_number", "campaign", "status", "attempt_count", "sent_at", "created_at")
    list_filter = ("status", "message_type", "created_at")
    search_fields = ("to_phone_number", "provider_message_id", "contact__name", "campaign__name")
    date_hierarchy = "created_at"
    inlines = [MessageStatusEventInline]
    readonly_fields = tuple(field.name for field in Message._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
