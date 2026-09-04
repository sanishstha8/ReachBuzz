"""
The channels a message can go out over.

One module, imported by contacts, campaigns, messaging and the providers, so
that "which channel?" is asked with the same vocabulary everywhere and adding a
third one is a change in a single place.

**A channel is not a provider.** WhatsApp is a channel; Meta's Cloud API is a
provider for it. SMS is a channel; whichever gateway sends it is a provider.
Keeping them separate is what lets a customer change gateway without their
campaigns changing meaning.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Channel(models.TextChoices):
    WHATSAPP = "whatsapp", _("WhatsApp")
    SMS = "sms", _("SMS")


#: The channel everything used before there was a choice. Every existing
#: campaign, message and consent record means this one, which is why it is the
#: default on all three and why the backfills do not have to guess.
DEFAULT_CHANNEL = Channel.WHATSAPP


#: How a channel describes itself when explaining a refusal to somebody.
CHANNEL_LABELS = {
    Channel.WHATSAPP: "WhatsApp",
    Channel.SMS: "SMS",
}


def label_for(channel: str) -> str:
    return CHANNEL_LABELS.get(channel, channel)
