from django.contrib import admin

from whatsapp.accounts import MessagingAccount
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


@admin.register(MessagingAccount)
class MessagingAccountAdmin(admin.ModelAdmin):
    """
    Per-organization senders.

    The access token is write-only. It is not a model field — it is a property
    over the ciphertext — so it appears here only because this form puts it
    there deliberately, and it is never rendered back. What an operator sees is
    the last four characters, which identifies the token without being it.
    """

    list_display = (
        "__str__",
        "organization",
        "provider",
        "phone_number_id",
        "status",
        "is_default",
        "token_display",
    )
    list_filter = ("status", "provider", "is_default")
    search_fields = (
        "label",
        "phone_number_id",
        "waba_id",
        "display_phone_number",
        "organization__name",
    )
    autocomplete_fields = ("organization",)
    readonly_fields = ("token_display", "verified_at", "last_error", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("organization", "provider", "label", "is_default", "status")}),
        (
            "Credentials",
            {
                "fields": ("phone_number_id", "waba_id", "access_token_input", "token_display"),
                "description": (
                    "The access token is stored encrypted and never shown again. "
                    "Leave the field empty to keep the current one."
                ),
            },
        ),
        (
            "Reported by the provider",
            {"fields": ("display_phone_number", "verified_name", "verified_at", "last_error")},
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Access token")
    def token_display(self, obj: MessagingAccount) -> str:
        return obj.token_hint if obj.pk else "—"

    def get_form(self, request, obj=None, **kwargs):
        """
        Add a write-only field for the token.

        Deliberately not a ``ModelForm`` field over ``access_token_encrypted``:
        that would render the ciphertext into the page, and a ciphertext in a
        browser history is a credential in a browser history.
        """
        from django import forms

        form = super().get_form(request, obj, **kwargs)

        class TokenForm(form):
            access_token_input = forms.CharField(
                label="Access token",
                required=False,
                widget=forms.PasswordInput(render_value=False),
                help_text="Leave empty to keep the stored token.",
            )

            def save(self, commit=True):
                instance = super().save(commit=False)
                entered = self.cleaned_data.get("access_token_input")
                if entered:
                    instance.access_token = entered
                if commit:
                    instance.save()
                return instance

        return TokenForm
