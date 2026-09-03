"""
The payment provider's webhook endpoint.

The second unauthenticated, CSRF-exempt route in this application, and it
follows the same rules as the first (``whatsapp.webhooks``) because the same
things are true of it: the caller has no session, no CSRF token, and will retry
a non-200 for days.

What is different is the consequence of getting it wrong. A mishandled delivery
report is a wrong status on a dashboard. A mishandled payment notification is
somebody's money credited twice, or not at all.

So, in order:

**The signature is the authentication.** Nothing is stored, parsed or queued
until the provider's HMAC over the *raw body* verifies. The endpoint is public;
anything that writes on unverified input is a way for a stranger to fill the
database, and here it would be a way to forge a payment.

**A replay is recognised and dropped.** ``event_id`` is unique, so the second
delivery of a notification is stored zero times and processed zero times. The
uniqueness is enforced by the database rather than by a lookup, because a
lookup has a race and a constraint does not.

**It answers 200 fast and does the work elsewhere.** Answering 200 says the
payload arrived intact and is stored, not that it was understood. A processing
failure is recorded on the event; asking the provider to redeliver would not
fix a bug on our side, and each redelivery is another chance to double-credit.
"""

from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from billing.invoicing import PaymentWebhookEvent, PaymentWebhookStatus
from billing.providers.factory import get_provider, provider_name
from core.audit import client_ip
from core.exceptions import ProviderNotConfigured

logger = logging.getLogger(__name__)

#: Anything larger is not a payment notification. Reading an unbounded body from
#: an unauthenticated endpoint is a way to be handed a very large one.
MAX_BODY_BYTES = 512 * 1024


@method_decorator(csrf_exempt, name="dispatch")
class PaymentWebhookView(View):
    """Receives, verifies, stores, and queues. Nothing else happens inline."""

    def post(self, request: HttpRequest) -> HttpResponse:
        body = request.body

        if len(body) > MAX_BODY_BYTES:
            logger.warning("Oversized payment webhook (%s bytes) from %s", len(body), client_ip(request))
            return HttpResponseForbidden("Payload too large.")

        try:
            provider = get_provider()
        except ProviderNotConfigured:
            logger.exception("Payment webhook arrived with no usable provider configured")
            return HttpResponseForbidden("Not configured.")

        if not provider.verify_webhook(body=body, headers=dict(request.headers)):
            logger.warning("Rejected an unsigned payment webhook from %s", client_ip(request))
            return HttpResponseForbidden("Invalid signature.")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Signed but unreadable. Worth knowing about — it means a provider
            # changed something — but there is nothing to store or replay.
            logger.warning("Signed payment webhook was not valid JSON")
            return JsonResponse({"status": "ignored"})

        if not isinstance(payload, dict):
            return JsonResponse({"status": "ignored"})

        events = provider.parse_webhook(payload)
        if not events:
            return JsonResponse({"status": "ignored"})

        queued = 0
        for parsed in events:
            if self._store(parsed, payload):
                queued += 1

        return JsonResponse({"status": "received", "queued": queued})

    def _store(self, parsed, payload: dict) -> bool:
        """
        Persist one event and queue it. Returns whether it was new.

        The ``IntegrityError`` branch is the replay case, and it is caught
        rather than pre-checked on purpose: two simultaneous redeliveries would
        both pass a ``.exists()`` check and both insert.

        The inner ``atomic`` is load-bearing. A failed statement poisons the
        transaction it ran in, so without a savepoint of its own the second of
        two events in one delivery could not be stored after the first was
        rejected as a replay — and under ``ATOMIC_REQUESTS`` the whole
        response would fail.
        """
        from billing.tasks import process_payment_webhook

        try:
            with transaction.atomic():
                event = PaymentWebhookEvent.objects.create(
                    provider=provider_name(),
                    event_id=parsed.event_id,
                    event_type=parsed.event_type,
                    payload=payload,
                    signature_valid=True,
                    status=PaymentWebhookStatus.RECEIVED,
                )
        except IntegrityError:
            logger.info("Payment webhook %s already received; ignoring the replay", parsed.event_id)
            return False

        process_payment_webhook.delay(str(event.pk))
        return True

    def get(self, request: HttpRequest) -> HttpResponse:
        """
        No verification handshake, unlike Meta's.

        Providers that need one add it here; answering 200 to an unauthenticated
        GET by default would tell a scanner this endpoint exists and works.
        """
        return HttpResponseForbidden("This endpoint accepts POST only.")
