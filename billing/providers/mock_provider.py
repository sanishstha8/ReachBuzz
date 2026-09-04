"""
Simulated payment provider.

Lets invoicing, collection and reconciliation be built and tested before any
merchant account exists. **No money moves and no card details are accepted** —
this provider has no concept of a card, which is deliberate: a mock that took
card numbers would be a mock that could leak them.

It simulates the failures that actually matter when integrating a real one: a
decline (never retry — retrying annoys the customer, alarms their bank, and on
some networks counts against the merchant), and a transient gateway error
(retry). Being able to exercise both paths locally is the point; a collection
loop whose error handling has never run is a collection loop that does not work.

**It honours idempotency keys.** A key it has already seen returns the original
result rather than a second charge. Real providers behave this way, and a mock
that did not would let a double-charge bug pass every test and appear in
production.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import random
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from django.conf import settings

from billing.providers.base import ChargeResult, PaymentEvent, PaymentProvider

logger = logging.getLogger(__name__)

# Weighted like the WhatsApp mock: mostly succeeds, fails in the ways that
# have consequences. Codes are shaped like real gateway codes without
# pretending to be any particular provider's documented values.
SIMULATED_FAILURES = [
    ("mock_card_declined", "Simulated decline. The customer's bank refused it.", False),
    ("mock_gateway_error", "Simulated temporary gateway error.", True),
]


class MockPaymentProvider(PaymentProvider):
    name = "mock"
    is_simulated = True

    def __init__(self) -> None:
        self.failure_rate = float(getattr(settings, "MOCK_PAYMENT_FAILURE_RATE", 0.0))
        self.secret = getattr(settings, "PAYMENT_WEBHOOK_SECRET", "") or "mock-secret"
        # Keyed by idempotency key. Process-local, which is all a mock needs -
        # it makes the replay behaviour testable without inventing storage.
        self._charges: dict[str, ChargeResult] = {}

    def charge(
        self,
        *,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> ChargeResult:
        if not idempotency_key:
            raise ValueError("An idempotency key is required to charge.")

        seen = self._charges.get(idempotency_key)
        if seen is not None:
            logger.info("Mock charge replayed for key %s; returning the first result", idempotency_key)
            return seen

        if amount <= 0:
            # Nothing to collect is a success with no reference: there is no
            # charge to reconcile later, and inventing one would leave a
            # payment row pointing at money that never moved.
            result = ChargeResult.ok("", amount, currency, raw={"simulated": True, "zero": True})
        elif random.random() < self.failure_rate:
            code, message, retryable = random.choice(SIMULATED_FAILURES)
            result = ChargeResult.failure(
                code, message, retryable=retryable, raw={"simulated": True}
            )
        else:
            result = ChargeResult.ok(
                f"mock_{uuid.uuid4().hex[:24]}",
                amount,
                currency,
                raw={"simulated": True, "description": description, "metadata": metadata or {}},
            )

        self._charges[idempotency_key] = result
        return result

    # -- Webhooks -----------------------------------------------------------

    def signature_for(self, body: bytes) -> str:
        """Sign a payload the way this provider would. Used by tests and the seeder."""
        return hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()

    def verify_webhook(self, *, body: bytes, headers: dict) -> bool:
        """
        Compared as bytes, and in constant time.

        Both matter. ``hmac.compare_digest`` raises TypeError on non-ASCII
        str input, which turns an attacker-supplied header into a 500 instead
        of a 403 - a bug this project has already had once, in the WhatsApp
        webhook, and does not intend to have again.
        """
        provided = headers.get("X-Mock-Signature") or headers.get("HTTP_X_MOCK_SIGNATURE") or ""
        if not provided:
            return False
        expected = self.signature_for(body)
        return hmac.compare_digest(expected.encode(), str(provided).encode())

    def parse_webhook(self, payload: dict) -> list[PaymentEvent]:
        reference = payload.get("reference") or ""
        event_type = payload.get("type") or ""
        if not reference or event_type not in {
            PaymentEvent.SUCCEEDED,
            PaymentEvent.FAILED,
            PaymentEvent.REFUNDED,
        }:
            return []

        raw_amount = payload.get("amount")
        occurred = payload.get("occurred_at")

        return [
            PaymentEvent(
                event_id=payload.get("id") or f"mock_evt_{reference}",
                event_type=event_type,
                reference=reference,
                amount=Decimal(str(raw_amount)) if raw_amount is not None else None,
                currency=payload.get("currency", ""),
                occurred_at=(
                    datetime.fromisoformat(occurred)
                    if occurred
                    else datetime.now(tz=UTC)
                ),
                raw=payload,
            )
        ]
