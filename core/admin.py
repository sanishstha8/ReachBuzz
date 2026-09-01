from django.contrib import admin

from core.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """The audit trail is append-only: read-only in the admin, by design."""

    list_display = ("created_at", "action", "user", "object_type", "object_id", "ip_address")
    list_filter = ("action", "created_at")
    search_fields = ("object_id", "description", "user__username", "user__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
