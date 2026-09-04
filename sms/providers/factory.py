"""
SMS provider selection.

    SMS_PROVIDER=mock   # simulated, nothing leaves the machine

Only the mock is registered, for the same reason only the mock payment provider
is: a real gateway needs an account, a registered sender id and, in several
countries, regulatory paperwork. A half-written integration against one is worse
than an honest absence.

Nothing outside ``sms.providers`` imports a concrete provider class.
"""

from __future__ import annotations

import logging

from django.conf import settings

from core.exceptions import ProviderNotConfigured
from sms.providers.base import SmsProvider
from sms.providers.mock_provider import MockSmsProvider

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, type[SmsProvider]] = {
    "mock": MockSmsProvider,
}


def get_provider(name: str | None = None) -> SmsProvider:
    """Build the configured provider. Not cached; see whatsapp.services.factory."""
    name = (name or provider_name()).strip().lower()

    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise ProviderNotConfigured(
            f"Unknown SMS_PROVIDER '{name}'. Valid values: {', '.join(sorted(PROVIDERS))}."
        )

    return provider_class()


def provider_name() -> str:
    return (getattr(settings, "SMS_PROVIDER", "mock") or "mock").strip().lower()


def is_simulated() -> bool:
    return get_provider().is_simulated
