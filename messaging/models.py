"""
Per-recipient message records.

One row per (campaign, contact), created before anything is sent. That row is
the unit of work, the idempotency anchor, and the audit record all at once:

* ``unique(campaign, contact)`` means a campaign cannot message anyone twice.
* The status field is claimed with a conditional UPDATE before sending, so a
  duplicated queue job finds nothing to do.
* ``provider_message_id`` maps Meta's webhook callbacks back to this row.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from campaigns.models import Campaign, CampaignMessageType
from contacts.models import Contact
from core.models import BaseModel, TimeStampedModel
from organizations.scoping import OrganizationOwnedModel, OrganizationScopedQuerySet
from whatsapp.models import MessageTemplate


class MessageStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    QUEUED = "queued", _("Queued")
    SENDING = "sending", _("Sending")
    SENT = "sent", _("Sent")
    DELIVERED = "delivered", _("Delivered")
    READ = "read", _("Read")
    FAILED = "failed", _("Failed")


# Status is monotonic: a late "sent" callback must never overwrite "read".
# Higher rank wins; FAILED is terminal and is handled separately.
STATUS_RANK: dict[str, int] = {
    MessageStatus.PENDING: 0,
    MessageStatus.QUEUED: 1,
    MessageStatus.SENDING: 2,
    MessageStatus.SENT: 3,
    MessageStatus.DELIVERED: 4,
    MessageStatus.READ: 5,
    MessageStatus.FAILED: 6,
}

# Statuses a worker is still expected to act on.
IN_FLIGHT_STATUSES = frozenset(
    {MessageStatus.PENDING, MessageStatus.QUEUED, MessageStatus.SENDING}
)
TERMINAL_STATUSES = frozenset(
    {MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ, MessageStatus.FAILED}
)
# Statuses from which a send may legitimately be attempted.
CLAIMABLE_STATUSES = frozenset({MessageStatus.PENDING, MessageStatus.QUEUED})


class MessageQuerySet(OrganizationScopedQuerySet):
    def in_flight(self) -> MessageQuerySet:
        return self.filter(status__in=IN_FLIGHT_STATUSES)

    def terminal(self) -> MessageQuerySet:
        return self.filter(status__in=TERMINAL_STATUSES)

    def failed(self) -> MessageQuerySet:
        return self.filter(status=MessageStatus.FAILED)

    def delivered_or_better(self) -> MessageQuerySet:
        return self.filter(status__in=[MessageStatus.DELIVERED, MessageStatus.READ])


class Message(OrganizationOwnedModel, BaseModel):
    """A single outbound WhatsApp message and everything known about it."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="messages")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="messages")

    # Snapshot: the number as it was when the campaign launched, so history
    # stays accurate even if the contact's number is later corrected.
    to_phone_number = models.CharField(max_length=20)

    message_type = models.CharField(
        max_length=16,
        choices=CampaignMessageType.choices,
        default=CampaignMessageType.TEMPLATE,
    )
    template = models.ForeignKey(
        MessageTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    template_name = models.CharField(max_length=512, blank=True)
    template_language = models.CharField(max_length=16, blank=True)

    # Resolved variable values and the exact text this recipient gets.
    rendered_payload = models.JSONField(default=dict, blank=True)

    provider_message_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
        help_text=_("The provider's message id (Meta calls this a wamid)."),
    )

    status = models.CharField(
        max_length=16, choices=MessageStatus.choices, default=MessageStatus.PENDING, db_index=True
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)

    error_code = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    error_details = models.JSONField(default=dict, blank=True)

    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    objects = MessageQuerySet.as_manager()

    class Meta:
        ordering = ["contact__name"]
        verbose_name = _("message")
        verbose_name_plural = _("messages")
        constraints = [
            # The idempotency anchor: a campaign reaches each contact once.
            models.UniqueConstraint(
                fields=["campaign", "contact"], name="unique_message_per_campaign_contact"
            ),
            # Provider ids must be unique, but many rows legitimately have none
            # yet, so the constraint skips blanks.
            models.UniqueConstraint(
                fields=["provider_message_id"],
                condition=~models.Q(provider_message_id=""),
                name="unique_provider_message_id",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="message_campaign_status_idx"),
            models.Index(fields=["status", "next_retry_at"], name="message_retry_idx"),
            models.Index(fields=["contact", "-created_at"], name="message_contact_recent_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.to_phone_number} · {self.get_status_display()}"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_claimable(self) -> bool:
        return self.status in CLAIMABLE_STATUSES

    @property
    def preview_text(self) -> str:
        return (self.rendered_payload or {}).get("text", "")

    def outranks(self, status: str) -> bool:
        """True when this message's status is already at or beyond ``status``."""
        return STATUS_RANK.get(self.status, 0) >= STATUS_RANK.get(status, 0)


class StatusEventSource(models.TextChoices):
    WEBHOOK = "webhook", _("Provider webhook")
    SEND_RESPONSE = "send_response", _("Provider send response")
    SYSTEM = "system", _("System")
    # Its own value on purpose: a simulated delivery must never be
    # indistinguishable from one a real provider reported.
    SIMULATED = "simulated", _("Simulated (mock provider)")


class MessageStatusEvent(TimeStampedModel):
    """
    Append-only log of every status the provider reported for a message.

    Kept separately from ``Message.status`` because callbacks arrive out of
    order and can be redelivered. The unique constraint makes replaying a
    webhook harmless.
    """

    id = models.BigAutoField(primary_key=True)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="status_events")
    status = models.CharField(max_length=16, choices=MessageStatus.choices)
    source = models.CharField(
        max_length=16, choices=StatusEventSource.choices, default=StatusEventSource.WEBHOOK
    )
    provider_timestamp = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = _("message status event")
        verbose_name_plural = _("message status events")
        constraints = [
            models.UniqueConstraint(
                fields=["message", "status", "provider_timestamp"],
                name="unique_status_event_per_message",
            ),
        ]
        indexes = [models.Index(fields=["message", "created_at"], name="status_event_lookup_idx")]

    def __str__(self) -> str:
        return f"{self.message_id} → {self.status}"
