"""
Choosing a sender for a message, and calling it.

The seam where two unlike providers meet. ``WhatsAppProvider`` and
``SmsProvider`` have different interfaces on purpose — SMS has no approved
template registry and nothing to sync — so nothing tries to make them one class.
What they share is the *result* shape, and that is enough: the Celery task needs
to know whether the send worked and whether to try again, and both answer that
in the same words.

So this module does the only two things that have to know about both:

* :func:`sender_for` resolves the right provider for a message's channel;
* :func:`send` translates a stored :class:`~messaging.models.Message` into
  whichever call that provider actually takes.

Everything else in the sending path stays channel-agnostic, which is why adding
SMS did not touch the retry logic, the rate limiter, the claim protocol or the
status machine.
"""

from __future__ import annotations

import logging

from campaigns.models import CampaignMessageType
from core.channels import Channel
from core.exceptions import ProviderNotConfigured

logger = logging.getLogger(__name__)


def sender_for(organization, channel: str):
    """
    The provider that will carry this message.

    WhatsApp resolves per organization, because Stage 5 gave each customer their
    own WABA. SMS does not yet: there is one configured gateway for the
    installation, and per-organization sender ids are a later job. Saying so
    here is better than a per-organization lookup that always returns the same
    thing and implies otherwise.
    """
    if channel == Channel.SMS:
        from sms.providers.factory import get_provider

        return get_provider()

    from whatsapp.services.factory import provider_for

    return provider_for(organization)


def send(message):
    """
    Ask the message's provider to deliver it.

    Returns whatever result object that provider produces. Both have
    ``success``, ``provider_message_id``, ``error_code``, ``error_message``,
    ``retryable`` and ``retry_after``, which is the whole of what the caller
    reads — so the caller does not branch on channel and has not had to learn
    that there is more than one.
    """
    provider = sender_for(message.organization, message.channel)

    if message.channel == Channel.SMS:
        return _send_sms(provider, message)
    return _send_whatsapp(provider, message)


def _send_sms(provider, message):
    """
    SMS carries text and nothing else.

    A template-typed message on the SMS channel is a configuration mistake
    rather than a per-message failure, and it is refused permanently: retrying
    it a thousand times would not make SMS grow a template registry.
    """
    body = (message.rendered_payload or {}).get("text", "")

    if not body and message.message_type == CampaignMessageType.TEMPLATE:
        from sms.providers.base import SmsResult

        return SmsResult.failure(
            "template_on_sms",
            "SMS has no approved-template concept; this campaign needs text.",
            retryable=False,
        )

    return provider.send_text(
        to=message.to_phone_number,
        body=body,
        sender_id=_sender_id_for(message),
    )


def _sender_id_for(message) -> str:
    """
    The originator the recipient sees.

    From settings for now, because SMS senders are installation-wide. Several
    networks require this to be pre-registered, so an empty value is normal and
    means "let the gateway decide" rather than "this is broken".
    """
    from django.conf import settings

    return getattr(settings, "SMS_SENDER_ID", "") or ""


def _send_whatsapp(provider, message):
    """Unchanged from the original ``whatsapp.tasks._send``; moved, not rewritten."""
    if message.message_type == CampaignMessageType.TEXT:
        return provider.send_text(
            to=message.to_phone_number,
            body=(message.rendered_payload or {}).get("text", ""),
        )

    values = (message.rendered_payload or {}).get("values", {})
    template = message.template
    ordered_tokens = list(template.variables or []) if template else list(values)

    return provider.send_template(
        to=message.to_phone_number,
        template_name=message.template_name,
        language=message.template_language,
        body_variables=[str(values.get(token, "")) for token in ordered_tokens],
    )


def preflight(organization, channel: str) -> None:
    """
    Check a channel can send before a campaign changes state.

    Raises :class:`~core.exceptions.ProviderNotConfigured`. Called at launch, so
    a missing gateway surfaces while the campaign is still a draft rather than
    after a thousand messages have been queued against nothing.
    """
    try:
        sender_for(organization, channel).check_configuration()
    except ProviderNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure here is a refusal
        raise ProviderNotConfigured(
            f"The {channel} channel is not usable: {exc}"
        ) from exc
