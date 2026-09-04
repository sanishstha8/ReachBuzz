"""
Campaigns.

A campaign is a plan: an audience, a message, and a lifecycle. It never holds
recipients directly — those are materialized as ``messaging.Message`` rows at
launch, one per contact, which is what makes a send resumable, pausable and
idempotent.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from contacts.models import ContactGroup
from core.channels import DEFAULT_CHANNEL, Channel
from core.models import BaseModel
from organizations.scoping import OrganizationOwnedModel, OrganizationScopedQuerySet
from whatsapp.models import MessageTemplate


class CampaignStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SCHEDULED = "scheduled", _("Scheduled")
    PROCESSING = "processing", _("Processing")
    PAUSED = "paused", _("Paused")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")


# The only permitted moves. Everything else raises InvalidStateTransition, so a
# new view cannot accidentally invent a path (e.g. relaunching a completed
# campaign, which would message everyone a second time).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    CampaignStatus.DRAFT: {
        CampaignStatus.SCHEDULED,
        CampaignStatus.PROCESSING,
        CampaignStatus.CANCELLED,
    },
    CampaignStatus.SCHEDULED: {
        CampaignStatus.DRAFT,
        CampaignStatus.PROCESSING,
        CampaignStatus.CANCELLED,
    },
    CampaignStatus.PROCESSING: {
        CampaignStatus.PAUSED,
        CampaignStatus.COMPLETED,
        CampaignStatus.FAILED,
        CampaignStatus.CANCELLED,
    },
    CampaignStatus.PAUSED: {
        CampaignStatus.PROCESSING,
        CampaignStatus.CANCELLED,
        CampaignStatus.COMPLETED,
    },
    CampaignStatus.COMPLETED: set(),
    CampaignStatus.FAILED: {CampaignStatus.CANCELLED},
    CampaignStatus.CANCELLED: set(),
}

TERMINAL_STATUSES = frozenset(
    {CampaignStatus.COMPLETED, CampaignStatus.FAILED, CampaignStatus.CANCELLED}
)
EDITABLE_STATUSES = frozenset({CampaignStatus.DRAFT, CampaignStatus.SCHEDULED})


class CampaignMessageType(models.TextChoices):
    TEMPLATE = "template", _("Approved template")
    TEXT = "text", _("Free-form text (service window only)")


class VariableSource(models.TextChoices):
    """Where a template placeholder's value comes from."""

    CONTACT_FIELD = "contact_field", _("Contact field")
    LITERAL = "literal", _("Fixed text")


class CampaignQuerySet(OrganizationScopedQuerySet):
    def active(self) -> CampaignQuerySet:
        return self.filter(status__in=[CampaignStatus.PROCESSING, CampaignStatus.PAUSED])

    def editable(self) -> CampaignQuerySet:
        return self.filter(status__in=EDITABLE_STATUSES)

    def search(self, term: str) -> CampaignQuerySet:
        term = (term or "").strip()
        if not term:
            return self
        return self.filter(models.Q(name__icontains=term) | models.Q(description__icontains=term))


class Campaign(OrganizationOwnedModel, BaseModel):
    name = models.CharField(max_length=150, db_index=True)
    description = models.TextField(blank=True)

    # Which channel this campaign goes out over. Chosen once, at creation, and
    # not changed afterwards: the audience was resolved against this channel's
    # consent, so switching it would send to people who never agreed to be
    # reached that way.
    channel = models.CharField(
        _("channel"),
        max_length=16,
        choices=Channel.choices,
        default=DEFAULT_CHANNEL,
        db_index=True,
    )

    message_type = models.CharField(
        max_length=16,
        choices=CampaignMessageType.choices,
        default=CampaignMessageType.TEMPLATE,
    )
    template = models.ForeignKey(
        MessageTemplate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="campaigns",
    )
    # Only used for CampaignMessageType.TEXT, which WhatsApp permits solely
    # inside the customer-service window.
    body_text = models.TextField(blank=True)

    # {"token": {"source": "contact_field"|"literal", "value": "name"|"SALE20"}}
    variable_mapping = models.JSONField(default=dict, blank=True)

    audience_groups = models.ManyToManyField(
        ContactGroup,
        through="CampaignAudience",
        related_name="campaigns",
        blank=True,
    )
    target_all_eligible = models.BooleanField(
        default=False,
        help_text=_("Target every opted-in, active contact instead of specific groups."),
    )

    status = models.CharField(
        max_length=16, choices=CampaignStatus.choices, default=CampaignStatus.DRAFT, db_index=True
    )
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Snapshot of the recipient count at launch, so the number shown later
    # cannot drift as contacts opt out afterwards.
    total_recipients = models.PositiveIntegerField(default=0)
    failure_reason = models.CharField(max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_campaigns",
    )

    objects = CampaignQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("campaign")
        verbose_name_plural = _("campaigns")
        indexes = [
            models.Index(fields=["status", "-created_at"], name="campaign_status_recent_idx"),
            models.Index(fields=["scheduled_at"], name="campaign_scheduled_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    # -- State ---------------------------------------------------------------

    @property
    def is_editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def can_launch(self) -> bool:
        return self.status in {CampaignStatus.DRAFT, CampaignStatus.SCHEDULED}

    @property
    def can_pause(self) -> bool:
        return self.status == CampaignStatus.PROCESSING

    @property
    def can_resume(self) -> bool:
        return self.status == CampaignStatus.PAUSED

    @property
    def can_cancel(self) -> bool:
        return not self.is_terminal

    def allowed_transitions(self) -> set[str]:
        return ALLOWED_TRANSITIONS.get(self.status, set())

    # -- Message ------------------------------------------------------------

    @property
    def uses_template(self) -> bool:
        return self.message_type == CampaignMessageType.TEMPLATE

    @property
    def required_variables(self) -> list[str]:
        if self.uses_template and self.template:
            return list(self.template.variables or [])
        return []

    @property
    def unmapped_variables(self) -> list[str]:
        mapping = self.variable_mapping or {}
        return [
            token
            for token in self.required_variables
            if not (mapping.get(token) or {}).get("value")
        ]


class CampaignAudience(models.Model):
    """Through model linking a campaign to the groups it targets."""

    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="audience_entries"
    )
    group = models.ForeignKey(
        ContactGroup, on_delete=models.PROTECT, related_name="campaign_entries"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "group"], name="unique_group_per_campaign"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.group.name} in {self.campaign.name}"
