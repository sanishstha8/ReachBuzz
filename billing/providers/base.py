"""
The payment provider contract.

Everything the rest of the application knows about taking money is on this
interface. Concrete providers translate it into whatever their API wants;
nothing above this line knows about Stripe's payment intents or Khalti's
lookup calls, and changing ``PAYMENT_PROVIDER`` changes no application code.

Deliberately the same shape as :mod:`whatsapp.services.base`, which has proved
itself across a mock and a real implementation. Two provider seams that look
alike is one idea to learn instead of two.

Three rules are baked into the types rather than left to each implementation:

**Money is Decimal, never float.** ``0.1 + 0.2`` is a rounding error everywhere
else in software and a discrepancy on an invoice here. Providers that speak in
minor units convert at their own boundary, which is the only place that knows
whether a currency has two decimal places or none.

**Every charge carries an idempotency key.** Networks retry, Celery retries, and
people double-click. A charge without a key is a charge that can happen twice,
and the second one is somebody's money.

**Providers report, they do not decide.** A provider returns what happened;
whether an invoice is now paid is settled by :mod:`billing.payments` against
the invoice's own state. A provider that could mark its own charges settled
would make a replayed webhook indistinguishable from a second payment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ChargeResult:
    """
    The outcome of one attempt to take money.

    ``retryable`` separates "the network hiccuped" from "the card was
    declined". Retrying a decline annoys the customer, alarms their bank, and
    on some networks counts against the merchant.
    """

    success: bool
    provider_reference: str = ""
    amount: Decimal = Decimal("0.00")
    currency: str = ""
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    #: Where to send the customer when the provider needs them (3-D Secure,
    #: a hosted checkout page). Empty when the charge settled outright.
    redirect_url: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        provider_reference: str,
        amount: Decimal,
        currency: str,
        raw: dict | None = None,
    ) -> ChargeResult:
        return cls(
            success=True,
            provider_reference=provider_reference,
            amount=amount,
            currency=currency,
            raw=raw or {},
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        error_message: str,
        *,
        retryable: bool = False,
        provider_reference: str = "",
        raw: dict | None = None,
    ) -> ChargeResult:
        return cls(
            success=False,
            provider_reference=provider_reference,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            raw=raw or {},
        )


@dataclass(frozen=True)
class PaymentEvent:
    """
    Something a provider is telling us happened, parsed out of a webhook.

    ``reference`` is the provider's own id for the charge and is what ties the
    event back to a payment we already know about. ``event_id`` is the id of
    the *notification*, which is what makes a redelivery recognisable — the two
    are different, and conflating them is how a retried webhook gets treated as
    a second payment.
    """

    event_id: str
    event_type: str
    reference: str
    amount: Decimal | None = None
    currency: str = ""
    occurred_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    #: Event types every provider maps onto, so callers never switch on a
    #: provider-specific string.
    SUCCEEDED = "payment.succeeded"
    FAILED = "payment.failed"
    REFUNDED = "payment.refunded"


class PaymentProvider(ABC):
    """Base class every payment provider implements."""

    #: Short identifier matching the PAYMENT_PROVIDER setting.
    name: str = "base"

    #: True when no money actually moves.
    is_simulated: bool = False

    def check_configuration(self) -> None:
        """
        Raise :class:`~core.exceptions.ProviderNotConfigured` if this provider
        cannot operate with the current settings. Called before an invoice is
        charged, so a misconfiguration surfaces before an invoice is marked
        anything.
        """
        return None

    @abstractmethod
    def charge(
        self,
        *,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> ChargeResult:
        """
        Attempt to collect ``amount``.

        ``idempotency_key`` is required, not optional. A provider that receives
        the same key twice must return the original result rather than charging
        again — and one that cannot honour that must say so from
        :meth:`check_configuration` rather than silently double-charging.
        """

    def verify_webhook(self, *, body: bytes, headers: dict) -> bool:
        """
        Whether a webhook really came from this provider.

        Takes the **raw body**, not a parsed dict. Signatures are computed over
        exact bytes, and re-serializing JSON to check one is how a valid
        signature comes out wrong (or, worse, how an invalid one comes out
        right after a lenient parser has normalised something away).

        Defaults to False. A provider that has not implemented verification
        must not be treated as having passed it.
        """
        return False

    def parse_webhook(self, payload: dict) -> list[PaymentEvent]:
        """Translate a provider payload into events. Unknown shapes yield []."""
        return []
