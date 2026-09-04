"""
Payment provider selection.

One setting decides which implementation the whole application uses:

    PAYMENT_PROVIDER=mock   # simulated, no money moves

Nothing outside ``billing.providers`` imports a concrete provider class, which
is what keeps :mod:`billing.payments` free of any one gateway's vocabulary.

Only the mock is registered today. That is not an oversight: adding Stripe or
Khalti means merchant credentials, a webhook secret and a live account, none of
which exist yet, and a half-written integration against a real provider is
worse than an honest absence — see the same reasoning in section 22 of the
README for the Meta integration.
"""

from __future__ import annotations

import logging

from django.conf import settings

from billing.providers.base import PaymentProvider
from billing.providers.mock_provider import MockPaymentProvider
from core.exceptions import ProviderNotConfigured

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, type[PaymentProvider]] = {
    "mock": MockPaymentProvider,
}


def get_provider(name: str | None = None) -> PaymentProvider:
    """
    Build the configured provider.

    Not cached, matching ``whatsapp.services.factory``: the mock reads its
    failure rate from settings at construction and tests override that with
    ``override_settings``.
    """
    name = (name or provider_name()).strip().lower()

    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise ProviderNotConfigured(
            f"Unknown PAYMENT_PROVIDER '{name}'. Valid values: {', '.join(sorted(PROVIDERS))}."
        )

    return provider_class()


def provider_name() -> str:
    return (getattr(settings, "PAYMENT_PROVIDER", "mock") or "mock").strip().lower()


def is_simulated() -> bool:
    """Whether the active provider only pretends to move money."""
    return get_provider().is_simulated
