"""
Provider selection.

One setting decides which implementation the whole application uses:

    WHATSAPP_PROVIDER=mock   # simulated, nothing leaves the machine
    WHATSAPP_PROVIDER=meta   # the official Meta Cloud API

Nothing outside ``whatsapp.services`` imports a concrete provider class.
"""

from __future__ import annotations

import logging

from django.conf import settings

from core.exceptions import ProviderNotConfigured
from whatsapp.services.base import WhatsAppProvider
from whatsapp.services.meta_cloud_api import MetaWhatsAppProvider
from whatsapp.services.mock_provider import MockWhatsAppProvider

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, type[WhatsAppProvider]] = {
    "mock": MockWhatsAppProvider,
    "meta": MetaWhatsAppProvider,
}


def get_provider(name: str | None = None) -> WhatsAppProvider:
    """
    Build the configured provider.

    Deliberately *not* cached: the mock provider reads its failure rate and
    latency from settings at construction, and tests override those with
    ``override_settings``. Construction is trivial in both implementations.
    """
    name = (name or getattr(settings, "WHATSAPP_PROVIDER", "mock")).strip().lower()

    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise ProviderNotConfigured(
            f"Unknown WHATSAPP_PROVIDER '{name}'. Valid values: {', '.join(sorted(PROVIDERS))}."
        )

    return provider_class()


def provider_name() -> str:
    return (getattr(settings, "WHATSAPP_PROVIDER", "mock") or "mock").strip().lower()


def is_simulated() -> bool:
    """Whether the active provider only simulates delivery."""
    return provider_name() == "mock"
