"""
The inbound webhook endpoint.

This is the only unauthenticated, CSRF-exempt route in the application — Meta
calls it, and Meta has no session and no CSRF token. Everything here follows
from that.

**The signature is the authentication.** Nothing is stored, parsed or queued
until the HMAC over the raw request body verifies against the app secret. An
unverified body is not persisted at all: the endpoint is public, so anything
that writes on unverified input is a way for a stranger to fill the database.

**It answers 200 fast and does the work elsewhere.** Meta retries a non-200
with decreasing frequency for up to seven days, so an endpoint that parses,
matches and updates rows inline turns one slow query into a week of duplicate
deliveries. The payload is persisted, a task is queued, and the response
returns.

**Answering 200 is not a claim that the payload was understood** — only that it
arrived intact and is safely stored. Processing failures are recorded on the
event, not signalled to Meta, because asking Meta to redeliver would not fix a
bug on our side.
"""

from __future__ import annotations

import hmac
import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core.audit import client_ip
from core.exceptions import ProviderNotConfigured
from whatsapp.models import WebhookEvent, WebhookEventStatus
from whatsapp.services.factory import get_provider

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"


@method_decorator(csrf_exempt, name="dispatch")
class MetaWebhookView(View):
    """Meta's verification handshake (GET) and event deliveries (POST)."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """
        The subscription handshake.

        Meta sends ``hub.mode``, ``hub.verify_token`` and ``hub.challenge``.
        When the token matches the one we configured, the challenge is echoed
        back verbatim as plain text — anything else, including a JSON-wrapped
        challenge, fails verification.
        """
        expected = getattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "")
        mode = request.GET.get("hub.mode", "")
        token = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")

        if not expected:
            logger.error("Webhook verification attempted with META_WEBHOOK_VERIFY_TOKEN unset.")
            return HttpResponseForbidden("Webhook verification is not configured.")

        # compare_digest so a wrong token cannot be found one character at a
        # time by timing the responses — on bytes, because the str form raises
        # TypeError on non-ASCII input and this value comes from a stranger's
        # query string. A 500 where a 403 belongs is not an answer.
        if mode != "subscribe" or not hmac.compare_digest(
            token.encode("utf-8", "replace"), expected.encode("utf-8")
        ):
            logger.warning("Rejected webhook verification from %s", client_ip(request))
            return HttpResponseForbidden("Verification failed.")

        return HttpResponse(challenge, content_type="text/plain")

    def post(self, request: HttpRequest) -> HttpResponse:
        """Accept a signed event, store it, and hand it to a worker."""
        raw_body = request.body

        try:
            provider = get_provider()
            verified = provider.verify_webhook_signature(
                raw_body, request.headers.get(SIGNATURE_HEADER, "")
            )
        except ProviderNotConfigured as exc:
            logger.error("Webhook received but the provider is not configured: %s", exc.message)
            return JsonResponse({"detail": "Webhook processing is not configured."}, status=503)
        except NotImplementedError:
            # The mock provider has no signatures to verify. Accepting the
            # payload anyway would mean the one endpoint a stranger can reach
            # behaves differently depending on a setting.
            logger.error("Webhook received while the mock provider is active; refusing.")
            return JsonResponse({"detail": "Webhooks require the meta provider."}, status=503)

        if not verified:
            logger.warning("Rejected webhook with an invalid signature from %s", client_ip(request))
            return HttpResponseForbidden("Invalid signature.")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Correctly signed but not JSON: genuinely Meta's, and genuinely
            # unusable. Recorded so it is visible, and not retried.
            logger.exception("Webhook payload was signed but could not be parsed as JSON.")
            WebhookEvent.objects.create(
                payload={},
                signature_valid=True,
                status=WebhookEventStatus.FAILED,
                error_message="Payload was not valid JSON.",
            )
            return JsonResponse({"status": "unparseable"}, status=200)

        event = WebhookEvent.objects.create(payload=payload, signature_valid=True)

        from whatsapp.tasks import process_webhook_event_task

        try:
            process_webhook_event_task.delay(str(event.pk))
        except Exception:
            # The payload is already safe on disk, so this is recoverable: the
            # periodic sweep picks up anything left unprocessed. Meta must not
            # be asked to redeliver something we have already stored.
            logger.exception("Could not queue webhook event %s; the sweep will retry it.", event.pk)

        return JsonResponse({"status": "accepted"}, status=200)
