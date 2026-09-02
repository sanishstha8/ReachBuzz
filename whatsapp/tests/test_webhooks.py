"""
The inbound webhook endpoint.

This is the only route in the application that a stranger can reach: no
session, no CSRF token, no authentication. So most of what is tested here is
refusal — that an unsigned or wrongly signed body changes nothing at all, and
that the one thing standing between the internet and our database is an HMAC
that actually gets checked.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from django.test import Client
from django.urls import reverse

from whatsapp.models import WebhookEvent, WebhookEventStatus

pytestmark = pytest.mark.django_db

APP_SECRET = "topsecretappsecret"
VERIFY_TOKEN = "the-token-we-configured"


@pytest.fixture(autouse=True)
def meta_configured(settings):
    settings.WHATSAPP_PROVIDER = "meta"
    settings.META_API_VERSION = "vTEST"
    settings.META_ACCESS_TOKEN = "EAAtoken"
    settings.META_PHONE_NUMBER_ID = "123"
    settings.META_APP_SECRET = APP_SECRET
    settings.META_WEBHOOK_VERIFY_TOKEN = VERIFY_TOKEN
    return settings


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client: Client, payload: dict | bytes, *, secret: str | None = APP_SECRET, header=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {}
    if header is not None:
        headers["HTTP_X_HUB_SIGNATURE_256"] = header
    elif secret is not None:
        headers["HTTP_X_HUB_SIGNATURE_256"] = sign(body, secret)

    return client.post(
        reverse("whatsapp-webhook"), data=body, content_type="application/json", **headers
    )


STATUS_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WABA",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": "123"},
                        "statuses": [
                            {
                                "id": "wamid.UNKNOWN",
                                "status": "delivered",
                                "timestamp": "1749416383",
                                "recipient_id": "9779800000001",
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}


class TestVerificationHandshake:
    def test_the_challenge_is_echoed_when_the_token_matches(self, client: Client) -> None:
        response = client.get(
            reverse("whatsapp-webhook"),
            {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"},
        )

        assert response.status_code == 200
        # Verbatim plain text: Meta compares the body exactly, so a JSON
        # wrapper or a trailing newline fails the subscription.
        assert response.content == b"1158201444"
        assert response["Content-Type"].startswith("text/plain")

    def test_a_wrong_token_is_refused(self, client: Client) -> None:
        response = client.get(
            reverse("whatsapp-webhook"),
            {"hub.mode": "subscribe", "hub.verify_token": "guessed", "hub.challenge": "123"},
        )
        assert response.status_code == 403

    def test_the_wrong_mode_is_refused(self, client: Client) -> None:
        response = client.get(
            reverse("whatsapp-webhook"),
            {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "123"},
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("token", ["é", "тест", "🙂", "a" * 500])
    def test_a_non_ascii_token_is_refused_rather_than_crashing(
        self, client: Client, token: str
    ) -> None:
        """
        hmac.compare_digest raises TypeError on non-ASCII strings, and this
        value comes straight from a stranger's query string. Comparing as
        bytes keeps the answer a 403 instead of a 500.
        """
        response = client.get(
            reverse("whatsapp-webhook"),
            {"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "123"},
        )

        assert response.status_code == 403

    def test_an_unconfigured_token_refuses_rather_than_matching_the_empty_string(
        self, client: Client, settings
    ) -> None:
        """Otherwise a blank setting would let anyone complete the handshake."""
        settings.META_WEBHOOK_VERIFY_TOKEN = ""

        response = client.get(
            reverse("whatsapp-webhook"),
            {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "123"},
        )

        assert response.status_code == 403


class TestSignature:
    def test_a_correctly_signed_payload_is_accepted(self, client: Client) -> None:
        response = post(client, STATUS_PAYLOAD)

        assert response.status_code == 200
        assert WebhookEvent.objects.count() == 1

    def test_an_unsigned_payload_is_refused_and_stored_nowhere(self, client: Client) -> None:
        """
        The endpoint is public. Persisting unverified bodies would let anyone
        fill the database by POSTing to a URL they can guess.
        """
        response = post(client, STATUS_PAYLOAD, secret=None)

        assert response.status_code == 403
        assert WebhookEvent.objects.count() == 0

    def test_a_payload_signed_with_the_wrong_secret_is_refused(self, client: Client) -> None:
        response = post(client, STATUS_PAYLOAD, secret="not-the-app-secret")

        assert response.status_code == 403
        assert WebhookEvent.objects.count() == 0

    def test_a_tampered_body_is_refused(self, client: Client) -> None:
        """A signature for a different body must not carry over to this one."""
        signature = sign(json.dumps({"object": "other"}).encode())

        response = post(client, STATUS_PAYLOAD, header=signature)

        assert response.status_code == 403
        assert WebhookEvent.objects.count() == 0

    @pytest.mark.parametrize("header", ["", "garbage", "sha1=abc", "sha256="])
    def test_a_malformed_signature_header_is_refused(self, client: Client, header: str) -> None:
        assert post(client, STATUS_PAYLOAD, header=header).status_code == 403

    @pytest.mark.parametrize("header", ["sha256=é", "sha256=тест", "sha256=🙂"])
    def test_a_non_ascii_signature_is_refused_rather_than_crashing(
        self, client: Client, header: str
    ) -> None:
        """Same trap as the verify token: compare on bytes, answer 403."""
        assert post(client, STATUS_PAYLOAD, header=header).status_code == 403
        assert WebhookEvent.objects.count() == 0

    def test_the_signature_covers_the_body_exactly_as_sent(self, client: Client) -> None:
        """
        Whitespace is part of the signed bytes. If the view ever parsed the
        JSON before verifying, this padded body would stop verifying.
        """
        body = b'{"object":   "whatsapp_business_account",\n  "entry": []}'

        response = post(client, body)

        assert response.status_code == 200


class TestAcceptance:
    def test_no_authentication_is_required(self, client: Client) -> None:
        """Meta has no session; requiring one would break every delivery."""
        assert post(client, STATUS_PAYLOAD).status_code == 200

    def test_no_csrf_token_is_required(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        assert post(csrf_client, STATUS_PAYLOAD).status_code == 200

    def test_the_untouched_payload_is_what_gets_stored(self, client: Client) -> None:
        """Our reading of an event is not evidence; the event is."""
        post(client, STATUS_PAYLOAD)

        assert WebhookEvent.objects.get().payload == STATUS_PAYLOAD

    def test_the_event_is_marked_as_having_verified(self, client: Client) -> None:
        post(client, STATUS_PAYLOAD)
        assert WebhookEvent.objects.get().signature_valid is True

    def test_a_signed_payload_that_is_not_json_is_recorded_not_retried(
        self, client: Client
    ) -> None:
        """
        Genuinely Meta's, and genuinely unusable. A non-200 would earn a week
        of redeliveries of something we can never parse.
        """
        response = post(client, b"this is not json")

        assert response.status_code == 200
        event = WebhookEvent.objects.get()
        assert event.status == WebhookEventStatus.FAILED
        assert event.payload == {}

    def test_the_mock_provider_refuses_rather_than_accepting_unverifiable_input(
        self, client: Client, settings
    ) -> None:
        """
        The mock has no signature scheme. Accepting the payload anyway would
        mean the one endpoint a stranger can reach behaves differently
        depending on a setting.
        """
        settings.WHATSAPP_PROVIDER = "mock"

        response = post(client, STATUS_PAYLOAD)

        assert response.status_code == 503
        assert WebhookEvent.objects.count() == 0

    def test_a_missing_app_secret_refuses_rather_than_accepting(
        self, client: Client, settings
    ) -> None:
        settings.META_APP_SECRET = ""

        response = post(client, STATUS_PAYLOAD)

        assert response.status_code == 503
        assert WebhookEvent.objects.count() == 0

    def test_no_credential_appears_in_any_response(self, client: Client) -> None:
        bodies = [
            post(client, STATUS_PAYLOAD).content,
            post(client, STATUS_PAYLOAD, secret=None).content,
            client.get(reverse("whatsapp-webhook"), {"hub.mode": "subscribe"}).content,
        ]

        for body in bodies:
            assert APP_SECRET.encode() not in body
            assert VERIFY_TOKEN.encode() not in body


class TestProcessingIsHandedOff:
    def test_the_payload_is_processed(self, client: Client) -> None:
        """Celery runs inline under the test settings, so this completes here."""
        post(client, STATUS_PAYLOAD)

        event = WebhookEvent.objects.get()
        assert event.status == WebhookEventStatus.PROCESSED
        assert event.processed_at is not None

    def test_a_queueing_failure_still_answers_200(self, client: Client, monkeypatch) -> None:
        """
        The payload is already stored, so asking Meta to redeliver would be
        asking for a duplicate of something we have. The sweep picks it up.
        """
        from whatsapp import tasks

        def explode(*args, **kwargs):
            raise RuntimeError("broker down")

        monkeypatch.setattr(tasks.process_webhook_event_task, "delay", explode)

        response = post(client, STATUS_PAYLOAD)

        assert response.status_code == 200
        event = WebhookEvent.objects.get()
        assert event.status == WebhookEventStatus.RECEIVED

    def test_the_sweep_picks_up_what_the_queue_missed(self, client: Client, monkeypatch) -> None:
        from whatsapp import tasks

        monkeypatch.setattr(
            tasks.process_webhook_event_task, "delay", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
        )
        post(client, STATUS_PAYLOAD)
        monkeypatch.undo()

        requeued = tasks.process_pending_webhooks_task()

        assert requeued == 1
        assert WebhookEvent.objects.get().status == WebhookEventStatus.PROCESSED

    def test_a_processing_error_is_recorded_on_the_event_not_signalled_to_meta(
        self, client: Client, monkeypatch
    ) -> None:
        """A bug on our side must not earn a week of duplicate deliveries."""
        from whatsapp.services import inbound

        monkeypatch.setattr(
            inbound, "process_event", lambda event: (_ for _ in ()).throw(ValueError("bad parse"))
        )

        response = post(client, STATUS_PAYLOAD)

        assert response.status_code == 200
        event = WebhookEvent.objects.get()
        assert event.status == WebhookEventStatus.FAILED
        assert "bad parse" in event.error_message

    def test_reprocessing_an_already_processed_event_is_a_no_op(self, client: Client) -> None:
        """Meta redelivers, and so does our own sweep."""
        from whatsapp.tasks import process_webhook_event_task

        post(client, STATUS_PAYLOAD)
        event = WebhookEvent.objects.get()

        assert process_webhook_event_task(str(event.pk)) == "already-processed"
