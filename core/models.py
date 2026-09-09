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

    # Money. An invoice that was issued, settled or cancelled with no trace is
    # an invoice nobody can reconcile against anything.
    INVOICE_ISSUED = "invoice_issued", "Invoice issued"
    INVOICE_PAID = "invoice_paid", "Invoice paid"
    INVOICE_VOIDED = "invoice_voided", "Invoice voided"

    # Somebody who works for the platform opened a customer's account. Reading
    # across the tenant boundary is a privacy event, not a page view, so
    # "who has looked at this customer?" needs an answer.
    BACKOFFICE_VIEWED = "backoffice_viewed", "Customer account viewed by staff"

    # Team membership. An organization's seats are billed and limited, so who
    # was invited, who accepted, and who was removed needs the same trail as
    # anything else that changes what a customer is entitled to.
    MEMBER_INVITED = "member_invited", "Invited a team member"
    INVITATION_ACCEPTED = "invitation_accepted", "Accepted an invitation"
    INVITATION_REVOKED = "invitation_revoked", "Revoked an invitation"
    MEMBER_REMOVED = "member_removed", "Removed a team member"


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


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class NotificationKind(models.TextChoices):
    """
    The things worth telling somebody about.

    Each is an event the system already knows and, until Stage 8, kept to
    itself. Adding one means adding a default below, deliberately, rather than
    inheriting "on" from a blanket setting.
    """

    CAMPAIGN_FINISHED = "campaign_finished", "A campaign finished sending"
    CAMPAIGN_FAILED = "campaign_failed", "A campaign finished with heavy failures"
    QUOTA_WARNING = "quota_warning", "A plan limit is nearly spent"
    QUOTA_REACHED = "quota_reached", "A plan limit has been reached"
    PAYMENT_FAILED = "payment_failed", "A payment did not go through"
    INVOICE_ISSUED = "invoice_issued", "An invoice was issued"
    SENDER_PROBLEM = "sender_problem", "A WhatsApp sender stopped working"


NOTIFICATION_KINDS = frozenset(NotificationKind.values)

#: Kinds only an owner or administrator hears about. A member who cannot change
#: the card does not need to be told it was declined.
ADMIN_ONLY_KINDS = frozenset(
    {
        NotificationKind.PAYMENT_FAILED,
        NotificationKind.INVOICE_ISSUED,
        NotificationKind.QUOTA_REACHED,
        NotificationKind.QUOTA_WARNING,
    }
)

#: Whether each kind is on for somebody who has never expressed a preference.
#:
#: Set per kind rather than all-on. The test is whether a customer would be
#: annoyed to receive it unasked: being told a payment failed is a service,
#: being told every campaign finished is a mailing list. So the ones that
#: require action default on, and the ones that are merely informational
#: default off.
NOTIFICATION_DEFAULTS = {
    NotificationKind.CAMPAIGN_FINISHED: False,
    NotificationKind.CAMPAIGN_FAILED: True,
    NotificationKind.QUOTA_WARNING: True,
    NotificationKind.QUOTA_REACHED: True,
    NotificationKind.PAYMENT_FAILED: True,
    NotificationKind.INVOICE_ISSUED: False,
    NotificationKind.SENDER_PROBLEM: True,
}


class NotificationPreferenceQuerySet(models.QuerySet):
    def wants(self, user, kind: str) -> bool:
        """
        Whether this user should receive this kind.

        An absent row means the default for that kind, not "yes". Nobody is
        opted in to anything by having never been asked \u2014 the same rule the
        contacts app applies to consent, applied to our own mail.
        """
        row = self.filter(user=user, kind=kind).first()
        if row is not None:
            return row.enabled
        return NOTIFICATION_DEFAULTS.get(kind, False)


class NotificationPreference(models.Model):
    """One person's answer for one kind of notification."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    kind = models.CharField(max_length=32, choices=NotificationKind.choices)
    enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = NotificationPreferenceQuerySet.as_manager()

    class Meta:
        verbose_name = "notification preference"
        verbose_name_plural = "notification preferences"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind"], name="unique_notification_preference"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user}: {self.get_kind_display()} {'on' if self.enabled else 'off'}"


class NotificationLog(models.Model):
    """
    What was sent, to whom, and why it will not be sent again.

    ``idempotency_key`` is unique and is the whole reason this table exists. A
    campaign that finishes while a worker is retrying, a webhook redelivered by
    a payment provider, a periodic job that runs twice after an outage \u2014 each
    would otherwise produce a duplicate email, and the person receiving them
    cannot tell a retry from a real second event.

    The row is written *before* the mail goes out. Sending and then recording
    would send twice whenever the recording failed, which is the wrong way round
    for something a person receives.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    kind = models.CharField(max_length=32, choices=NotificationKind.choices, db_index=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    subject = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    #: Null means the row was claimed but the mail never went. Worth being able
    #: to find: it is the difference between "we told them" and "we meant to".
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "notification"
        verbose_name_plural = "notifications"
        indexes = [
            models.Index(fields=["user", "-created_at"], name="notification_user_recent_idx"),
        ]

    def __str__(self) -> str:
        state = "sent" if self.sent_at else "not sent"
        return f"{self.get_kind_display()} to {self.user} ({state})"
