"""
The provider contract.

Everything the rest of the application knows about "sending a WhatsApp message"
is on this interface. Concrete providers translate it into whatever their API
actually wants; nothing above this line knows about Meta's payload shapes, and
swapping ``WHATSAPP_PROVIDER`` changes no application code.

The interface is deliberately neutral about payload structure — it takes an
ordered list of variable *values*, not a provider-specific component tree — so
that the Meta implementation in Phase 7 can be written against Meta's real
documentation without this file having guessed at it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SendResult:
    """
    The outcome of one send attempt.

    ``retryable`` is what the Celery task consults: a transient failure (rate
    limit, timeout, provider 5xx) is worth another attempt, while a permanent
    one (not a WhatsApp user, template rejected) is not, and retrying it would
    just burn quota.
    """

    success: bool
    provider_message_id: str = ""
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    retry_after: int | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, provider_message_id: str, raw: dict | None = None) -> SendResult:
        return cls(success=True, provider_message_id=provider_message_id, raw=raw or {})

    @classmethod
    def failure(
        cls,
        error_code: str,
        error_message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        raw: dict | None = None,
    ) -> SendResult:
        return cls(
            success=False,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            retry_after=retry_after,
            raw=raw or {},
        )


@dataclass(frozen=True)
class TemplateData:
    """A template as reported by the provider, for the sync in Phase 7."""

    name: str
    language: str
    category: str
    status: str
    body_text: str
    header_text: str = ""
    footer_text: str = ""
    provider_template_id: str = ""
    rejection_reason: str = ""


@dataclass(frozen=True)
class InboundStatus:
    """A delivery status the provider reported for a message we sent."""

    provider_message_id: str
    status: str
    timestamp: datetime | None = None
    error_code: str = ""
    error_message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InboundMessage:
    """A message a recipient sent to us (used for STOP handling in Phase 7)."""

    from_phone_number: str
    text: str
    provider_message_id: str = ""
    timestamp: datetime | None = None
    #: The *business* number the message arrived on, as the provider's own id.
    #: This is what says which customer it belongs to: one webhook URL serves
    #: every tenant, and two customers can hold the same person as a contact.
    #: Without it, an inbound STOP could withdraw the wrong customer's consent.
    business_phone_number_id: str = ""
    raw: dict = field(default_factory=dict)


class WhatsAppProvider(ABC):
    """Base class every provider implements."""

    #: Short identifier matching the WHATSAPP_PROVIDER setting.
    name: str = "base"

    #: True when messages are simulated rather than actually delivered.
    is_simulated: bool = False

    # -- Configuration ------------------------------------------------------

    def check_configuration(self) -> None:
        """
        Raise :class:`~core.exceptions.ProviderNotConfigured` if this provider
        cannot operate with the current settings. Called before a campaign
        launches, so a misconfiguration surfaces before any state changes.
        """
        return None

    # -- Sending ------------------------------------------------------------

    @abstractmethod
    def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language: str,
        body_variables: Sequence[str] = (),
        header_variables: Sequence[str] = (),
    ) -> SendResult:
        """Send an approved template message to a single recipient."""

    @abstractmethod
    def send_text(self, *, to: str, body: str) -> SendResult:
        """
        Send a free-form text message.

        WhatsApp permits this only inside the customer-service window that
        opens when the recipient messages the business first.
        """

    # -- Templates ----------------------------------------------------------

    def fetch_templates(self) -> list[TemplateData]:
        """Return the templates the provider knows about."""
        raise NotImplementedError(f"{self.name} does not support template sync.")

    # -- Webhooks (implemented in Phase 7) ----------------------------------

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """Whether ``raw_body`` genuinely came from the provider."""
        raise NotImplementedError(f"{self.name} does not support webhooks.")

    def parse_webhook(self, payload: dict) -> tuple[list[InboundStatus], list[InboundMessage]]:
        """Split a webhook payload into status updates and inbound messages."""
        raise NotImplementedError(f"{self.name} does not support webhooks.")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.__class__.__name__} name={self.name!r}>"
