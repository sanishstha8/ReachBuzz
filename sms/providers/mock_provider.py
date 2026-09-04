"""
Simulated SMS gateway.

Nothing leaves the machine. Same role as the WhatsApp and payment mocks, and
the same honesty: it simulates the failures that have consequences rather than
always succeeding, because a retry path that has never run is a retry path that
does not work.

The failures modelled are the ones that actually differ from WhatsApp's. A
number that is not reachable on WhatsApp is a permanent WhatsApp failure; the
same number may be perfectly reachable by SMS. And SMS adds one WhatsApp does
not have at all: a message rejected because the sender id is not registered on
that network, which is permanent until somebody does paperwork.
"""

from __future__ import annotations

import logging
import random
import uuid

from django.conf import settings

from core.phone import is_valid_phone_number
from sms.providers.base import SmsProvider, SmsResult, segment_count

logger = logging.getLogger(__name__)

SIMULATED_FAILURES = [
    ("mock_carrier_rejected", "Simulated carrier rejection. Not retried.", False, None),
    ("mock_sender_id_unregistered", "Simulated: sender id not registered on this network.", False, None),
    ("mock_gateway_throttled", "Simulated gateway rate limit. The send will be retried.", True, 5),
    ("mock_gateway_error", "Simulated temporary gateway error. The send will be retried.", True, None),
]


class MockSmsProvider(SmsProvider):
    name = "mock"
    is_simulated = True

    def __init__(self) -> None:
        self.failure_rate = float(getattr(settings, "MOCK_SMS_FAILURE_RATE", 0.0))

    def send_text(self, *, to: str, body: str, sender_id: str = "") -> SmsResult:
        if not is_valid_phone_number(to):
            # Permanent, and caught here rather than at the gateway: a number
            # that is not a number will not become one on a retry.
            return SmsResult.failure(
                "invalid_number", "That is not a valid phone number.", retryable=False
            )

        if not body.strip():
            return SmsResult.failure(
                "empty_body", "An SMS with no text is not a message.", retryable=False
            )

        segments = segment_count(body)

        if random.random() < self.failure_rate:  # noqa: S311
            code, message, retryable, retry_after = random.choice(SIMULATED_FAILURES)  # noqa: S311
            return SmsResult.failure(
                code, message, retryable=retryable, retry_after=retry_after
            )

        # Logged with the number masked, matching the WhatsApp mock: a phone
        # number in a log file is personal data in a log file.
        logger.info(
            "Mock SMS to \u2026%s (%s segment%s)",
            to[-4:],
            segments,
            "" if segments == 1 else "s",
        )
        return SmsResult.ok(
            f"mock_sms_{uuid.uuid4().hex[:20]}",
            segments,
            raw={"simulated": True, "sender_id": sender_id, "segments": segments},
        )
