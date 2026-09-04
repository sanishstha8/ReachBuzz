"""
Per-organization messaging credentials.

Until Stage 5 there was one WhatsApp Business Account for the whole
installation, read from the environment. That is right for a single business
running its own copy and wrong for a platform: a customer's messages must go out
from *their* number, count against *their* messaging limit, and stop when *they*
disconnect.

**The split is deliberate, and it is the same split a real BSP uses.**

| Where | What | Why |
|---|---|---|
| Environment | App id, app secret, webhook verify token, API version | One Meta App serves every tenant. The app secret signs *all* inbound webhooks, so it cannot be per-tenant — see below |
| Database, encrypted | Access token, phone number id, WABA id | These are the customer's. Their number, their token, their limit |

The webhook is why this split is not arbitrary. Meta delivers every tenant's
events to one URL, signed with the *app* secret. Verification therefore has to
happen before we know which organization an event belongs to — you cannot look
up a per-tenant secret using a payload you have not yet authenticated. Routing
to the right organization happens afterwards, by the ``phone_number_id`` Meta
includes in the payload, which is why that column is unique across the table.

**Access tokens are encrypted at rest.** See :mod:`core.encryption` for what
that does and does not buy. The plaintext is never stored, never logged, never
serialized, and never rendered — the property the rest of this project already
holds for the environment credentials.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.encryption import DecryptionFailed, decrypt, encrypt
from core.models import BaseModel
from organizations.scoping import OrganizationOwnedModel, OrganizationScopedQuerySet

logger = logging.getLogger(__name__)


class MessagingAccountStatus(models.TextChoices):
    UNVERIFIED = "unverified", _("Not verified yet")
    ACTIVE = "active", _("Active")
    DISABLED = "disabled", _("Disabled")


class MessagingAccountQuerySet(OrganizationScopedQuerySet):
    def usable(self) -> MessagingAccountQuerySet:
        return self.filter(status=MessagingAccountStatus.ACTIVE)

    def default_for(self, organization) -> MessagingAccount | None:
        """
        The account an organization sends from.

        Prefers the one marked default, then any active one. Returns ``None``
        rather than falling back to the environment — see
        :func:`account_for` for why that distinction matters.
        """
        return (
            self.for_organization(organization)
            .usable()
            .order_by("-is_default", "created_at")
            .first()
        )


class MessagingAccount(OrganizationOwnedModel, BaseModel):
    """
    One WhatsApp sender belonging to one organization.

    A foreign key rather than a one-to-one: a WABA can hold several numbers, and
    a business that sends order updates from one and support replies from
    another is an ordinary arrangement, not an edge case. Exactly one is the
    default, enforced by a partial unique index.
    """

    provider = models.CharField(
        _("provider"),
        max_length=32,
        default="meta",
        help_text=_("Matches a key in whatsapp.services.factory.PROVIDERS."),
    )

    label = models.CharField(
        _("label"), max_length=100, blank=True, help_text=_("What this number is for.")
    )

    #: Meta's id for the sending number. Unique across the table because inbound
    #: webhooks are routed by it — two organizations claiming one number would
    #: make delivery reports ambiguous.
    phone_number_id = models.CharField(_("phone number id"), max_length=64, unique=True)
    waba_id = models.CharField(_("WABA id"), max_length=64, blank=True)

    #: Informational, as reported by Meta. Never used for routing.
    display_phone_number = models.CharField(max_length=32, blank=True)
    verified_name = models.CharField(max_length=128, blank=True)

    #: Ciphertext. Read through `access_token`, never touched directly.
    access_token_encrypted = models.TextField(blank=True)

    status = models.CharField(
        max_length=16,
        choices=MessagingAccountStatus.choices,
        default=MessagingAccountStatus.UNVERIFIED,
        db_index=True,
    )
    is_default = models.BooleanField(default=True)

    verified_at = models.DateTimeField(null=True, blank=True)
    #: The last reason verification failed. Provider wording, never a credential.
    last_error = models.CharField(max_length=255, blank=True)

    objects = MessagingAccountQuerySet.as_manager()

    class Meta:
        ordering = ["-is_default", "created_at"]
        verbose_name = _("messaging account")
        verbose_name_plural = _("messaging accounts")
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_default=True),
                name="one_default_messaging_account_per_organization",
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status"], name="msgacct_org_status_idx"),
        ]

    def __str__(self) -> str:
        return self.label or self.display_phone_number or self.phone_number_id

    # -- The token ----------------------------------------------------------

    @property
    def access_token(self) -> str:
        """
        The decrypted token.

        A property rather than a field so that nothing can accidentally include
        it: it is absent from ``_meta.fields``, so a ``ModelForm``, a
        ``ModelSerializer``, ``values()`` and the admin's default field list all
        skip it unless somebody names it deliberately.
        """
        try:
            return decrypt(self.access_token_encrypted)
        except DecryptionFailed:
            logger.exception(
                "Could not decrypt the access token for messaging account %s", self.pk
            )
            raise

    @access_token.setter
    def access_token(self, value: str) -> None:
        self.access_token_encrypted = encrypt(value or "")

    @property
    def has_token(self) -> bool:
        return bool(self.access_token_encrypted)

    @property
    def token_hint(self) -> str:
        """
        Something to show an operator that identifies the token without being it.

        The last four characters only, and only for a token long enough that
        four characters do not narrow it down. Anything more is a credential in
        a template.
        """
        try:
            token = self.access_token
        except DecryptionFailed:
            return "unreadable"
        if len(token) < 12:
            return "set" if token else "not set"
        return f"…{token[-4:]}"

    # -- Validation ---------------------------------------------------------

    def clean(self):
        from whatsapp.services.factory import PROVIDERS

        if self.provider not in PROVIDERS:
            raise ValidationError({"provider": _("Unknown provider.")})

        # The mock provider sends nothing anywhere, so it needs no credentials.
        if self.provider != "mock" and not self.has_token:
            raise ValidationError(
                {"access_token_encrypted": _("A live provider needs an access token.")}
            )

    def mark_verified(self, *, name: str = "", number: str = "") -> None:
        self.status = MessagingAccountStatus.ACTIVE
        self.verified_at = timezone.now()
        self.last_error = ""
        if name:
            self.verified_name = name
        if number:
            self.display_phone_number = number
        self.save(
            update_fields=[
                "status",
                "verified_at",
                "last_error",
                "verified_name",
                "display_phone_number",
                "updated_at",
            ]
        )

    def mark_failed(self, reason: str) -> None:
        """
        Record why verification failed, without disabling the account.

        A transient Meta outage must not switch off a working sender. Disabling
        is a decision an operator or the customer makes.
        """
        self.last_error = reason[:255]
        self.save(update_fields=["last_error", "updated_at"])
