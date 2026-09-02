"""
WhatsApp message templates.

Meta requires business-initiated messages to use a template that Meta has
reviewed and approved. This model is a **mirror** of that registry, not a
substitute for it: templates are synced in (Phase 7), and nothing here submits
a template for approval or marks one approved on Meta's behalf.

To make the application developable before credentials exist, a template may
instead be created locally with ``source = LOCAL``. Local templates are
obviously labelled throughout the UI and are refused at launch time whenever
the live Meta provider is selected — see :meth:`MessageTemplate.usability`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel

# Matches Meta's {{1}} positional placeholders and {{name}} named ones alike.
VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class TemplateSource(models.TextChoices):
    SYNCED = "synced", _("Synced from Meta")
    LOCAL = "local", _("Local (development only)")


class TemplateStatus(models.TextChoices):
    """Mirrors the approval states Meta reports for a template."""

    APPROVED = "approved", _("Approved")
    PENDING = "pending", _("Pending review")
    REJECTED = "rejected", _("Rejected")
    PAUSED = "paused", _("Paused")
    DISABLED = "disabled", _("Disabled")
    NOT_SUBMITTED = "not_submitted", _("Not submitted to Meta")


class TemplateCategory(models.TextChoices):
    MARKETING = "marketing", _("Marketing")
    UTILITY = "utility", _("Utility")
    AUTHENTICATION = "authentication", _("Authentication")


@dataclass(frozen=True)
class Usability:
    """Whether a template may be used to send, and why not when it may not."""

    usable: bool
    reason: str = ""


class MessageTemplateQuerySet(models.QuerySet):
    def approved(self) -> MessageTemplateQuerySet:
        return self.filter(source=TemplateSource.SYNCED, status=TemplateStatus.APPROVED)

    def usable_with(self, provider: str) -> MessageTemplateQuerySet:
        """
        Templates that could be sent through ``provider``.

        With the mock provider, local templates are allowed so campaigns can be
        built and simulated. With the live Meta provider, only templates Meta
        has actually approved qualify.
        """
        if provider == "mock":
            return self.filter(
                models.Q(source=TemplateSource.SYNCED, status=TemplateStatus.APPROVED)
                | models.Q(source=TemplateSource.LOCAL)
            )
        return self.approved()


class MessageTemplate(BaseModel):
    """A WhatsApp message template and its variable placeholders."""

    name = models.CharField(
        max_length=512,
        help_text=_("The template name as registered with Meta, e.g. order_ready."),
    )
    language = models.CharField(
        max_length=16,
        default="en_US",
        help_text=_("Language and locale code, e.g. en_US."),
    )
    category = models.CharField(
        max_length=32, choices=TemplateCategory.choices, default=TemplateCategory.UTILITY
    )
    source = models.CharField(
        max_length=16, choices=TemplateSource.choices, default=TemplateSource.LOCAL
    )
    status = models.CharField(
        max_length=16,
        choices=TemplateStatus.choices,
        default=TemplateStatus.NOT_SUBMITTED,
        db_index=True,
    )

    header_text = models.CharField(max_length=60, blank=True)
    body_text = models.TextField(help_text=_("Use {{1}} or {{name}} for variables."))
    footer_text = models.CharField(max_length=60, blank=True)

    variables = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Ordered placeholder tokens found in the header and body."),
    )
    example_values = models.JSONField(default=dict, blank=True)

    # Meta's identifier for the template, populated by the Phase 7 sync.
    provider_template_id = models.CharField(max_length=128, blank=True, db_index=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_templates",
    )

    objects = MessageTemplateQuerySet.as_manager()

    class Meta:
        ordering = ["name", "language"]
        verbose_name = _("message template")
        verbose_name_plural = _("message templates")
        constraints = [
            models.UniqueConstraint(
                fields=["name", "language"], name="unique_template_name_language"
            ),
        ]
        indexes = [models.Index(fields=["source", "status"], name="template_usability_idx")]

    def __str__(self) -> str:
        return f"{self.name} ({self.language})"

    def save(self, *args, **kwargs):
        # The variable list is derived from the text, never hand-maintained,
        # so a preview can never disagree with what will actually be sent.
        self.variables = extract_variables(f"{self.header_text}\n{self.body_text}")
        super().save(*args, **kwargs)

    @property
    def is_local(self) -> bool:
        return self.source == TemplateSource.LOCAL

    @property
    def variable_count(self) -> int:
        return len(self.variables or [])

    @property
    def full_text(self) -> str:
        parts = [self.header_text, self.body_text, self.footer_text]
        return "\n\n".join(part for part in parts if part)

    def usability(self, provider: str | None = None) -> Usability:
        """Whether this template can be used to send through ``provider``."""
        provider = provider or getattr(settings, "WHATSAPP_PROVIDER", "mock")

        if self.source == TemplateSource.SYNCED:
            if self.status == TemplateStatus.APPROVED:
                return Usability(True)
            return Usability(
                False,
                f"Meta has not approved this template (status: {self.get_status_display()}).",
            )

        # Local template.
        if provider == "mock":
            return Usability(True)
        return Usability(
            False,
            "This is a local development template. Only templates approved by Meta can be "
            "sent through the live WhatsApp Cloud API. Submit it in WhatsApp Manager, then "
            "sync your templates.",
        )

    def is_usable(self, provider: str | None = None) -> bool:
        return self.usability(provider).usable


def extract_variables(text: str) -> list[str]:
    """
    Return the placeholder tokens in ``text``, in order, without duplicates.

    ``"Hello {{name}}, order {{order_id}} — thanks {{name}}"`` yields
    ``["name", "order_id"]``.
    """
    seen: list[str] = []
    for match in VARIABLE_PATTERN.finditer(text or ""):
        token = match.group(1)
        if token not in seen:
            seen.append(token)
    return seen


class WebhookEventStatus(models.TextChoices):
    RECEIVED = "received", _("Received")
    PROCESSED = "processed", _("Processed")
    FAILED = "failed", _("Failed")
    REJECTED = "rejected", _("Rejected (bad signature)")


class WebhookEvent(BaseModel):
    """
    A raw webhook delivery from Meta, stored before anything interprets it.

    Persisting first and processing afterwards is what lets the endpoint answer
    200 in milliseconds. That matters more than it sounds: Meta retries a
    non-200 with decreasing frequency for up to seven days, so an endpoint that
    does its work inline turns one slow database query into a week of duplicate
    deliveries.

    Keeping the untouched payload also means a parsing bug is replayable. The
    event is evidence; our reading of it is not.
    """

    payload = models.JSONField(default=dict, blank=True)
    signature_valid = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16,
        choices=WebhookEventStatus.choices,
        default=WebhookEventStatus.RECEIVED,
        db_index=True,
    )
    status_count = models.PositiveIntegerField(
        default=0, help_text=_("Delivery statuses found in this payload.")
    )
    message_count = models.PositiveIntegerField(
        default=0, help_text=_("Inbound messages found in this payload.")
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("webhook event")
        verbose_name_plural = _("webhook events")
        indexes = [models.Index(fields=["status", "-created_at"], name="webhook_status_recent_idx")]

    def __str__(self) -> str:
        return f"{self.get_status_display()} at {self.created_at:%Y-%m-%d %H:%M}"
