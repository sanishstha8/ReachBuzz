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


def get_provider(name: str | None = None, *, account=None) -> WhatsAppProvider:
    """
    Build a provider, optionally for one customer's messaging account.

    Deliberately *not* cached: the mock reads its failure rate from settings at
    construction, tests override those with ``override_settings``, and — since
    Stage 5 — caching would be actively dangerous, because a cached instance
    holds one tenant's access token and would hand it to the next caller.

    With no ``account``, credentials come from the environment. That is the
    single-tenant path, which is every installation that predates Stage 5.
    """
    if account is not None:
        name = (name or account.provider or "").strip().lower()

    name = (name or getattr(settings, "WHATSAPP_PROVIDER", "mock")).strip().lower()

    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise ProviderNotConfigured(
            f"Unknown WHATSAPP_PROVIDER '{name}'. Valid values: {', '.join(sorted(PROVIDERS))}."
        )

    if account is None or name == "mock":
        # The mock sends nothing anywhere, so whose credentials they are does
        # not arise. Constructing it with them would only risk logging them.
        return provider_class()

    return provider_class(
        access_token=account.access_token,
        phone_number_id=account.phone_number_id,
        waba_id=account.waba_id,
        account=account,
    )


def provider_for(organization) -> WhatsAppProvider:
    """
    The provider an organization sends through.

    Falls back to the environment when the organization has no messaging
    account of its own. **That fallback is what keeps every pre-Stage-5
    installation working**, and it is also the thing to think hardest about
    before running this as a public platform: it means a customer with no
    account of their own would send through the deployment's number, on the
    deployment's messaging limit and reputation.

    ``WHATSAPP_REQUIRE_MESSAGING_ACCOUNT=True`` turns the fallback off, so an
    organization without its own account cannot send at all. A platform serving
    strangers wants that on. A business running its own copy wants it off, which
    is why off is the default — the alternative would break every existing
    deployment on upgrade.
    """
    from whatsapp.accounts import MessagingAccount

    account = MessagingAccount.objects.default_for(organization)
    if account is not None:
        return get_provider(account=account)

    if getattr(settings, "WHATSAPP_REQUIRE_MESSAGING_ACCOUNT", False):
        raise ProviderNotConfigured(
            "This organization has no WhatsApp sender connected. Connect a "
            "WhatsApp Business Account before sending."
        )

    return get_provider()


def provider_name() -> str:
    return (getattr(settings, "WHATSAPP_PROVIDER", "mock") or "mock").strip().lower()


def is_simulated() -> bool:
    """Whether the active provider only simulates delivery."""
    return provider_name() == "mock"
