"""
Consent, one channel at a time.

Until Stage 9 there was one channel, so one boolean was the whole truth:
``Contact.opted_in`` meant "may be sent a WhatsApp message", because there was
nothing else to send. Adding SMS makes that boolean ambiguous, and the ambiguity
is not academic — resolving it the convenient way would mean **every contact who
agreed to WhatsApp order updates is silently opted in to SMS marketing.**

That is exactly the thing this project's first rule forbids. Consent is never
inferred, and consent to be messaged one way is not consent to be messaged
another. Somebody who accepted a delivery notification on WhatsApp has not
agreed to a text message, and in most jurisdictions the two are separately
regulated.

So consent is per channel, and :meth:`ContactQuerySet.eligible` takes one.

**Why WhatsApp is not a row in this table.** It could be, and one day it should
be. But ``Contact.opted_in`` carries every opt-in and opt-out this system has
ever recorded, each with a source and an audit entry, and migrating consent
state is the single riskiest data migration this codebase could run: getting it
wrong means messaging somebody who said no. So the existing column stays
authoritative for WhatsApp, this table carries every other channel, and
``eligible(channel)`` is the one place that knows which is which. Consolidating
them is a later job, done deliberately, not a side effect of adding SMS.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.channels import Channel


class ChannelConsentQuerySet(models.QuerySet):
    def for_channel(self, channel: str) -> ChannelConsentQuerySet:
        return self.filter(channel=channel)

    def granted(self) -> ChannelConsentQuerySet:
        return self.filter(opted_in=True)


class ContactChannelConsent(models.Model):
    """
    Whether one contact may be messaged over one channel.

    A row exists only once somebody has made a decision. **No row means no
    consent** — the absence of a record is a "no", never a "not asked yet, so
    probably fine". That is the same default the boolean has always had, kept
    deliberately: a channel added tomorrow starts with nobody opted in, and the
    only way anybody joins it is somebody recording that they agreed.
    """

    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, related_name="channel_consents"
    )
    channel = models.CharField(max_length=16, choices=Channel.choices, db_index=True)

    opted_in = models.BooleanField(default=False)
    #: Free-form because the reasons differ per channel; validated at the
    #: service layer, which is the only supported way to write these rows.
    source = models.CharField(max_length=32, blank=True)

    opted_in_at = models.DateTimeField(null=True, blank=True)
    opted_out_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ChannelConsentQuerySet.as_manager()

    class Meta:
        verbose_name = _("channel consent")
        verbose_name_plural = _("channel consents")
        constraints = [
            # One decision per contact per channel. Two rows disagreeing about
            # whether somebody said yes is not a state worth being able to reach.
            models.UniqueConstraint(
                fields=["contact", "channel"], name="unique_consent_per_contact_channel"
            )
        ]
        indexes = [
            models.Index(fields=["channel", "opted_in"], name="consent_channel_state_idx"),
        ]

    def __str__(self) -> str:
        state = "opted in" if self.opted_in else "opted out"
        return f"{self.contact_id} {state} of {self.get_channel_display()}"

    def grant(self, source: str) -> None:
        self.opted_in = True
        self.source = source
        self.opted_in_at = timezone.now()
        self.opted_out_at = None

    def withdraw(self, source: str) -> None:
        self.opted_in = False
        self.source = source
        self.opted_out_at = timezone.now()
