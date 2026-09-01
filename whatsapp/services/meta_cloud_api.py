"""
Meta WhatsApp Business Platform Cloud API provider.

**Implemented in Phase 7.** The methods below deliberately raise rather than
guess: endpoint paths, payload shapes, error codes and the webhook signature
scheme will be written against Meta's official documentation at implementation
time, not invented here. A plausible-looking wrong implementation is worse than
an honest gap — it would fail silently against the real API.

What is already settled and will not change:

* every credential is read from the environment (``META_*``), never hardcoded;
* :meth:`check_configuration` fails fast and by name when one is missing, so a
  misconfiguration surfaces before a campaign changes state;
* no credential is ever logged, echoed in an error, or returned by the API.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from django.conf import settings

from core.exceptions import ProviderNotConfigured
from whatsapp.services.base import (
    InboundMessage,
    InboundStatus,
    SendResult,
    TemplateData,
    WhatsAppProvider,
)

logger = logging.getLogger(__name__)

# Settings that must be present before this provider can be used at all.
REQUIRED_SETTINGS = (
    "META_API_VERSION",
    "META_ACCESS_TOKEN",
    "META_PHONE_NUMBER_ID",
)

# Additionally required for template sync and webhook verification.
REQUIRED_FOR_TEMPLATES = ("META_WABA_ID",)
REQUIRED_FOR_WEBHOOKS = ("META_APP_SECRET",)

NOT_IMPLEMENTED = (
    "The Meta Cloud API provider is implemented in Phase 7, against Meta's "
    "current official documentation. Set WHATSAPP_PROVIDER=mock to continue "
    "development in the meantime."
)


class MetaWhatsAppProvider(WhatsAppProvider):
    """Sends through Meta's official Cloud API."""

    name = "meta"
    is_simulated = False

    def __init__(self):
        self.api_version = getattr(settings, "META_API_VERSION", "")
        self.phone_number_id = getattr(settings, "META_PHONE_NUMBER_ID", "")
        self.waba_id = getattr(settings, "META_WABA_ID", "")
        self.timeout = getattr(settings, "WHATSAPP_REQUEST_TIMEOUT", 30)

    # -- Configuration ------------------------------------------------------

    def check_configuration(self) -> None:
        """Fail by name, listing what is missing — but never printing a value."""
        missing = [
            name for name in REQUIRED_SETTINGS if not getattr(settings, name, "")
        ]
        if missing:
            raise ProviderNotConfigured(
                "The Meta provider is missing required configuration: "
                f"{', '.join(missing)}. Set these in your environment."
            )

    # -- Sending ------------------------------------------------------------

    def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language: str,
        body_variables: Sequence[str] = (),
        header_variables: Sequence[str] = (),
    ) -> SendResult:
        self.check_configuration()
        raise NotImplementedError(NOT_IMPLEMENTED)

    def send_text(self, *, to: str, body: str) -> SendResult:
        self.check_configuration()
        raise NotImplementedError(NOT_IMPLEMENTED)

    # -- Templates ----------------------------------------------------------

    def fetch_templates(self) -> list[TemplateData]:
        self.check_configuration()
        if not self.waba_id:
            raise ProviderNotConfigured(
                "Template sync requires META_WABA_ID to be set."
            )
        raise NotImplementedError(NOT_IMPLEMENTED)

    # -- Webhooks -----------------------------------------------------------

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        if not getattr(settings, "META_APP_SECRET", ""):
            raise ProviderNotConfigured(
                "Webhook verification requires META_APP_SECRET to be set."
            )
        raise NotImplementedError(NOT_IMPLEMENTED)

    def parse_webhook(self, payload: dict) -> tuple[list[InboundStatus], list[InboundMessage]]:
        raise NotImplementedError(NOT_IMPLEMENTED)
