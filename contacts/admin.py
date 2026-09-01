from django.contrib import admin

from contacts.models import (
    Contact,
    ContactGroup,
    ContactImport,
    ContactImportRow,
    GroupMembership,
)


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 0
    autocomplete_fields = ("group",)
    readonly_fields = ("added_at", "added_by")


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "phone_number", "status", "opted_in", "opt_in_source", "created_at")
    list_filter = ("opted_in", "status", "opt_in_source", "created_at")
    search_fields = ("name", "phone_number", "email")
    readonly_fields = (
        "country_code",
        "opt_in_at",
        "opt_out_at",
        "last_error_code",
        "last_error_message",
        "created_at",
        "updated_at",
    )
    inlines = [GroupMembershipInline]
    date_hierarchy = "created_at"
    fieldsets = (
        (None, {"fields": ("name", "phone_number", "country_code", "email", "status", "notes")}),
        (
            "Consent",
            {
                "fields": (
                    "opted_in",
                    "opt_in_source",
                    "opt_in_at",
                    "opt_out_source",
                    "opt_out_at",
                ),
                "description": (
                    "Consent recorded for this contact. Prefer the opt-in / opt-out actions in "
                    "the application, which write an audit entry."
                ),
            },
        ),
        ("Delivery", {"fields": ("last_error_code", "last_error_message")}),
        ("Bookkeeping", {"fields": ("created_by", "created_at", "updated_at")}),
    )


@admin.register(ContactGroup)
class ContactGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "members", "eligible", "created_at")
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        # Annotate once for the whole changelist rather than querying per row.
        return super().get_queryset(request).with_counts()

    @admin.display(description="Members", ordering="member_count")
    def members(self, obj) -> int:
        return obj.member_count

    @admin.display(description="Can be messaged", ordering="eligible_count")
    def eligible(self, obj) -> int:
        return obj.eligible_count


class ContactImportRowInline(admin.TabularInline):
    model = ContactImportRow
    extra = 0
    can_delete = False
    readonly_fields = ("row_number", "outcome", "raw_data", "error_message", "contact")

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ContactImport)
class ContactImportAdmin(admin.ModelAdmin):
    list_display = (
        "file_name",
        "status",
        "total_rows",
        "imported_count",
        "duplicate_count",
        "invalid_count",
        "not_opted_in_count",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("file_name",)
    readonly_fields = tuple(
        field.name for field in ContactImport._meta.fields if field.name != "id"
    )
    inlines = [ContactImportRowInline]

    def has_add_permission(self, request) -> bool:
        return False
