from django.contrib import admin

from campaigns.models import Campaign, CampaignAudience


class CampaignAudienceInline(admin.TabularInline):
    model = CampaignAudience
    extra = 0
    autocomplete_fields = ("group",)
    readonly_fields = ("added_at",)


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "message_type", "total_recipients", "created_by", "created_at")
    list_filter = ("status", "message_type", "created_at")
    search_fields = ("name", "description")
    date_hierarchy = "created_at"
    inlines = [CampaignAudienceInline]
    readonly_fields = (
        "status",
        "total_recipients",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("name", "description", "created_by")}),
        ("Message", {"fields": ("message_type", "template", "body_text", "variable_mapping")}),
        ("Audience", {"fields": ("target_all_eligible",)}),
        (
            "Lifecycle",
            {
                "fields": (
                    "status",
                    "scheduled_at",
                    "total_recipients",
                    "started_at",
                    "completed_at",
                    "failure_reason",
                ),
                "description": (
                    "Status is read-only here. Use the campaign pages so every transition "
                    "goes through the state machine and is audited."
                ),
            },
        ),
    )
