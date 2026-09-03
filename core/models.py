"""Abstract base models and the cross-app audit trail."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class UUIDPrimaryKeyModel(models.Model):
    """Primary keys are UUIDs so identifiers can be exposed in URLs safely."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    """Adds automatic created_at / updated_at bookkeeping."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BaseModel(UUIDPrimaryKeyModel, TimeStampedModel):
    """The default base for domain models in this project."""

    class Meta:
        abstract = True


class AuditAction(models.TextChoices):
    """Actions worth keeping a permanent, queryable record of."""

    USER_REGISTERED = "user_registered", "Account registered"
    EMAIL_VERIFIED = "email_verified", "Email address verified"
    LOGIN = "login", "User logged in"
    LOGIN_FAILED = "login_failed", "Login attempt failed"
    LOGOUT = "logout", "User logged out"

    CONTACT_CREATED = "contact_created", "Contact created"
    CONTACT_UPDATED = "contact_updated", "Contact updated"
    CONTACT_DELETED = "contact_deleted", "Contact deleted"
    CONTACT_OPTED_IN = "contact_opted_in", "Contact opted in"
    CONTACT_OPTED_OUT = "contact_opted_out", "Contact opted out"
    CONTACTS_IMPORTED = "contacts_imported", "Contacts imported from CSV"

    CAMPAIGN_CREATED = "campaign_created", "Campaign created"
    CAMPAIGN_LAUNCHED = "campaign_launched", "Campaign launched"
    CAMPAIGN_PAUSED = "campaign_paused", "Campaign paused"
    CAMPAIGN_RESUMED = "campaign_resumed", "Campaign resumed"
    CAMPAIGN_CANCELLED = "campaign_cancelled", "Campaign cancelled"

    TEMPLATES_SYNCED = "templates_synced", "Templates synced from provider"

    # An export puts contact details and message history on someone's laptop.
    # That is a data-protection event, so it belongs in the same trail as a
    # consent change rather than only in a web server log.
    REPORT_EXPORTED = "report_exported", "Report exported"

    # What a customer is entitled to, and who changed it. A limit that moved
    # without a trace is indistinguishable from a limit that was never enforced.
    SUBSCRIPTION_STARTED = "subscription_started", "Subscription started"
    SUBSCRIPTION_CHANGED = "subscription_changed", "Subscription plan changed"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled", "Subscription cancelled"


class AuditLog(UUIDPrimaryKeyModel):
    """
    Append-only record of consent changes and campaign activity.

    Compliance requires being able to answer "who sent what, to how many
    people, when, and on what consent basis" long after the fact, so this table
    is never updated or deleted by application code.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=64, choices=AuditAction.choices, db_index=True)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    description = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "audit log entry"
        verbose_name_plural = "audit log"
        indexes = [
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self) -> str:
        actor = self.user.get_username() if self.user else "system"
        return f"{self.get_action_display()} by {actor} at {self.created_at:%Y-%m-%d %H:%M}"
