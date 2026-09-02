"""
The whole path, with Meta selected and its HTTP stubbed.

Every other test in this phase checks one seam. This checks that the seams meet:
a campaign launches, the Celery task calls the real provider code, Meta's
response is recorded, Meta's webhook comes back, and the campaign finishes.

It is the test that would have caught the integration bugs that unit tests
cannot — a payload the provider builds but the task never reaches, a wamid
recorded in a form the webhook lookup cannot match, a campaign that sends
perfectly and then sits at "processing" forever because nothing closes it.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import responses
from django.test import Client
from django.urls import reverse

from campaigns.models import CampaignStatus
from messaging.models import Message, MessageStatus
from whatsapp.models import WebhookEvent, WebhookEventStatus

pytestmark = pytest.mark.django_db

MESSAGES_URL = "https://graph.facebook.com/vTEST/123/messages"
APP_SECRET = "topsecretappsecret"


@pytest.fixture(autouse=True)
def meta_configured(settings):
    settings.WHATSAPP_PROVIDER = "meta"
    settings.META_API_VERSION = "vTEST"
    settings.META_ACCESS_TOKEN = "EAAsupersecrettoken1234567890"
    settings.META_PHONE_NUMBER_ID = "123"
    settings.META_APP_SECRET = APP_SECRET
    settings.META_WEBHOOK_VERIFY_TOKEN = "verify-me"
    return settings


def accept(wamid: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "9779800000001", "wa_id": "9779800000001"}],
        "messages": [{"id": wamid, "message_status": "accepted"}],
    }


def deliver(client: Client, wamid: str, status: str, errors=None) -> None:
    """Post a signed status webhook, exactly as Meta would."""
    entry = {"id": wamid, "status": status, "timestamp": "1749416383", "recipient_id": "977980000001"}
    if errors:
        entry["errors"] = errors

    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"value": {"statuses": [entry]}, "field": "messages"}]}],
        }
    ).encode()

    response = client.post(
        reverse("whatsapp-webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256="sha256="
        + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest(),
    )
    assert response.status_code == 200


class TestSendThenDeliver:
    def test_a_campaign_sends_through_meta_and_its_webhooks_confirm_delivery(
        self, launched_campaign, client: Client
    ) -> None:
        """
        Two separate lifecycles, and they are not the same thing.

        A *campaign* is complete once nothing is still in flight — our sending
        work is finished. A *message* keeps moving afterwards, as Meta reports
        what happened to it. That is why the delivery rate on the reports page
        is measured against messages the provider accepted rather than against
        campaign status.
        """
        launched_campaign.refresh_from_db()
        messages = list(Message.objects.filter(campaign=launched_campaign))

        assert len(messages) == 3
        assert {m.status for m in messages} == {MessageStatus.SENT}
        assert sorted(m.provider_message_id for m in messages) == [
            "wamid.0",
            "wamid.1",
            "wamid.2",
        ]
        # Handed over in full, so the sending work is done.
        assert launched_campaign.status == CampaignStatus.COMPLETED

        # --- Meta reports back, and the messages move on --------------------
        for index in range(3):
            deliver(client, f"wamid.{index}", "delivered")
        deliver(client, "wamid.0", "read")

        statuses = sorted(
            Message.objects.filter(campaign=launched_campaign).values_list("status", flat=True)
        )
        assert statuses == [MessageStatus.DELIVERED, MessageStatus.DELIVERED, MessageStatus.READ]
        # A finished campaign is not reopened by a late callback.
        launched_campaign.refresh_from_db()
        assert launched_campaign.status == CampaignStatus.COMPLETED

    def test_the_requests_meta_received_are_the_documented_shape(
        self, launched_campaign, http
    ) -> None:
        """The task's translation of a Message must survive to the wire."""
        body = json.loads(http.calls[0].request.body)

        assert body["messaging_product"] == "whatsapp"
        assert body["type"] == "template"
        assert body["template"]["name"] == "order_ready"
        assert body["to"].startswith("977")
        assert not body["to"].startswith("+")
        # The per-recipient variable mapping reached the payload.
        parameters = body["template"]["components"][0]["parameters"]
        assert {p["text"] for p in parameters} >= {"A-100"}

    @pytest.fixture
    def launched_campaign(self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks, http):
        """Launch under the meta provider, with three accepted sends stubbed."""
        from campaigns.services import launch_campaign

        for index in range(3):
            http.add(responses.POST, MESSAGES_URL, json=accept(f"wamid.{index}"), status=200)

        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)
        ready_campaign.refresh_from_db()
        return ready_campaign


class TestFailurePath:
    def test_a_permanent_provider_error_fails_the_message_without_retrying(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks, http
    ) -> None:
        from campaigns.services import launch_campaign

        error = {
            "error": {
                "message": "(#131026) Unable to deliver",
                "code": 131026,
                "error_data": {"details": "Receiver is not a WhatsApp user"},
                "fbtrace_id": "A",
            }
        }
        for _ in range(3):
            http.add(responses.POST, MESSAGES_URL, json=error, status=400)

        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        messages = Message.objects.filter(campaign=ready_campaign)
        assert {m.status for m in messages} == {MessageStatus.FAILED}
        assert {m.error_code for m in messages} == {"131026"}
        # Three recipients, three calls: a permanent error is not retried.
        assert len(http.calls) == 3

    def test_a_failure_webhook_marks_a_message_that_meta_accepted(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks, http, client
    ) -> None:
        """
        Acceptance is not delivery. Meta can take a message and then report it
        undeliverable minutes later, and that report is the real outcome.
        """
        from campaigns.services import launch_campaign

        for index in range(3):
            http.add(responses.POST, MESSAGES_URL, json=accept(f"wamid.{index}"), status=200)

        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        deliver(
            client,
            "wamid.0",
            "failed",
            errors=[{"code": 131026, "error_data": {"details": "Not a WhatsApp user"}}],
        )

        message = Message.objects.get(provider_message_id="wamid.0")
        assert message.status == MessageStatus.FAILED
        assert message.error_message == "Not a WhatsApp user"
        message.contact.refresh_from_db()
        assert message.contact.last_error_code == "131026"


class TestNoCredentialLeaks:
    def test_nothing_stored_from_a_send_contains_a_credential(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks, http, settings
    ) -> None:
        from campaigns.services import launch_campaign

        for index in range(3):
            http.add(responses.POST, MESSAGES_URL, json=accept(f"wamid.{index}"), status=200)

        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        stored = json.dumps(
            list(
                Message.objects.values(
                    "rendered_payload", "error_details", "error_message", "provider_message_id"
                )
            )
        )
        assert settings.META_ACCESS_TOKEN not in stored
        assert APP_SECRET not in stored

    def test_a_stored_webhook_event_holds_only_metas_payload(
        self, client: Client, launched_for_webhook
    ) -> None:
        deliver(client, "wamid.0", "delivered")

        event = WebhookEvent.objects.get()
        assert event.status == WebhookEventStatus.PROCESSED
        assert APP_SECRET not in json.dumps(event.payload)

    @pytest.fixture
    def launched_for_webhook(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks, http
    ):
        from campaigns.services import launch_campaign

        for index in range(3):
            http.add(responses.POST, MESSAGES_URL, json=accept(f"wamid.{index}"), status=200)
        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)
        return ready_campaign
