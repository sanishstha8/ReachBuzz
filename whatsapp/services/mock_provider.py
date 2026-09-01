"""
Simulated WhatsApp provider.

Lets the whole application be built, demonstrated and tested before Meta
credentials exist. Nothing leaves the machine.

The failure modes it simulates are modelled on the ones that actually matter
when integrating: a transient rate limit (retryable), a transient upstream
error (retryable), and a permanently undeliverable number (not retryable).
Being able to exercise the retry path locally is the point — a send loop whose
error handling has never run is a send loop that does not work.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Sequence

from django.conf import settings

from core.phone import is_valid_phone_number
from whatsapp.services.base import SendResult, TemplateData, WhatsAppProvider

logger = logging.getLogger(__name__)

# Simulated failures, weighted. Codes mirror the shape of real provider errors
# without pretending to be specific documented Meta codes.
SIMULATED_FAILURES = [
    (
        "mock_rate_limited",
        "Simulated provider rate limit. The send will be retried.",
        True,
        5,
    ),
    (
        "mock_upstream_error",
        "Simulated temporary provider error. The send will be retried.",
        True,
        None,
    ),
    (
        "mock_undeliverable",
        "Simulated permanent failure: this number is not reachable on WhatsApp.",
        False,
        None,
    ),
]


class MockWhatsAppProvider(WhatsAppProvider):
    """Generates plausible responses without contacting anything."""

    name = "mock"
    is_simulated = True

    def __init__(
        self,
        *,
        failure_rate: float | None = None,
        latency_seconds: float | None = None,
        seed: int | None = None,
    ):
        self.failure_rate = (
            failure_rate
            if failure_rate is not None
            else getattr(settings, "MOCK_PROVIDER_FAILURE_RATE", 0.0)
        )
        self.latency_seconds = (
            latency_seconds
            if latency_seconds is not None
            else getattr(settings, "MOCK_PROVIDER_LATENCY_SECONDS", 0.0)
        )
        # Simulation only — nothing here is security-sensitive.
        self._random = random.Random(seed)  # noqa: S311

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
        result = self._simulate(to)
        logger.info(
            "[mock] template %s (%s) to %s -> %s",
            template_name,
            language,
            _mask(to),
            "sent" if result.success else result.error_code,
        )
        return result

    def send_text(self, *, to: str, body: str) -> SendResult:
        result = self._simulate(to)
        logger.info(
            "[mock] text (%d chars) to %s -> %s",
            len(body or ""),
            _mask(to),
            "sent" if result.success else result.error_code,
        )
        return result

    # -- Templates ----------------------------------------------------------

    def fetch_templates(self) -> list[TemplateData]:
        """
        The mock provider has no upstream registry.

        Returning an empty list is honest here — unlike the Meta provider,
        there is genuinely nothing to sync, and local templates are created in
        the application instead.
        """
        return []

    # -- Internals ----------------------------------------------------------

    def _simulate(self, to: str) -> SendResult:
        if self.latency_seconds > 0:
            time.sleep(self.latency_seconds)

        # A number that is not valid E.164 would be rejected by a real
        # provider too, so the mock rejects it rather than reporting success.
        if not is_valid_phone_number(to):
            return SendResult.failure(
                "mock_invalid_number",
                f"'{to}' is not a valid phone number.",
                retryable=False,
            )

        if self.failure_rate > 0 and self._random.random() < self.failure_rate:
            code, message, retryable, retry_after = self._random.choice(SIMULATED_FAILURES)
            return SendResult.failure(
                code, message, retryable=retryable, retry_after=retry_after
            )

        return SendResult.ok(
            provider_message_id=f"wamid.MOCK.{uuid.uuid4().hex}",
            raw={"simulated": True},
        )


def _mask(number: str) -> str:
    """Log the tail of a number only — the full value is personal data."""
    number = str(number or "")
    return f"…{number[-4:]}" if len(number) > 4 else "…"
