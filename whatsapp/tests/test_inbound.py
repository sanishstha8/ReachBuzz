"""
What a webhook payload actually changes.

Two guarantees inherited from the messaging layer are load-bearing here, and
both are tested against real redelivery rather than assumed: applying a status
twice must change nothing the second time, and a callback that arrives late
must not drag a message backwards. Meta retries for up to seven days and makes
no ordering promise, so a system without both would corrupt its own history.

The consent tests are the other half. An inbound "STOP" is the one message a
recipient can send that changes their state, and getting it wrong in either
direction is a compliance failure: missing one keeps messaging someone who
asked us to stop, and a false positive silently ends a conversation they wanted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from campaigns.models import CampaignStatus
from contacts.models import OptOutSource
from core.models import AuditAction, AuditLog
from messaging.models import Message, MessageStatus, MessageStatusEvent, StatusEventSource
from whatsapp.models import WebhookEvent
from whatsapp.services.inbound import ProcessResult, is_stop_request, process_event

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def meta_configured(settings):
    settings.WHATSAPP_PROVIDER = "meta"
    settings.META_API_VERSION = "vTEST"
    settings.META_ACCESS_TOKEN = "EAAtoken"
    settings.META_PHONE_NUMBER_ID = "123"
    settings.META_APP_SECRET = "secret"
    return settings


@pytest.fixture
def sent_message(make_campaign, make_contact):
    """A message the provider has accepted, so a callback can match it."""
    contact = make_contact("Aarav", "+9779800000001", opted_in=True)
    return Message.objects.create(
        campaign=make_campaign("Summer", status=CampaignStatus.PROCESSING),
        contact=contact,
        to_phone_number=contact.phone_number,
        status=MessageStatus.SENT,
        provider_message_id="wamid.TEST123",
    )


def status_event(status: str, *, wamid="wamid.TEST123", errors=None, timestamp="1749416383"):
    entry = {"id": wamid, "status": status, "timestamp": timestamp, "recipient_id": "977980000001"}
    if errors:
        entry["errors"] = errors
    return WebhookEvent(
        payload={
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"value": {"statuses": [entry]}, "field": "messages"}]}],
        }
    )


def inbound_event(text: str, *, sender="9779800000001"):
    return WebhookEvent(
        payload={
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA",
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": sender,
                                        "id": "wamid.INBOUND",
                                        "timestamp": "1749416383",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ]
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
    )


class TestDeliveryStatuses:
    def test_a_delivery_report_advances_the_message(self, sent_message) -> None:
        result = process_event(status_event("delivered"))

        sent_message.refresh_from_db()
        assert result.statuses_applied == 1
        assert sent_message.status == MessageStatus.DELIVERED
        assert sent_message.delivered_at is not None

    def test_the_provider_timestamp_is_used_not_our_clock(self, sent_message) -> None:
        process_event(status_event("delivered", timestamp="1749416383"))

        sent_message.refresh_from_db()
        assert sent_message.delivered_at == datetime.fromtimestamp(1749416383, tz=UTC)

    def test_the_event_is_recorded_as_a_webhook_not_a_simulation(self, sent_message) -> None:
        """A real delivery and a mocked one must never be indistinguishable."""
        process_event(status_event("delivered"))

        assert MessageStatusEvent.objects.get().source == StatusEventSource.WEBHOOK

    def test_a_redelivered_webhook_changes_nothing_twice(self, sent_message) -> None:
        """Meta retries for up to seven days; duplicates are the normal case."""
        event = status_event("delivered")

        first = process_event(event)
        second = process_event(event)

        assert first.statuses_applied == 1
        assert second.statuses_applied == 0
        assert MessageStatusEvent.objects.count() == 1

    def test_a_late_callback_does_not_drag_the_message_backwards(self, sent_message) -> None:
        """Callbacks arrive out of order; "read" must survive a late "sent"."""
        process_event(status_event("read"))
        process_event(status_event("sent", timestamp="1749416000"))

        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.READ

    def test_a_status_for_a_message_we_never_sent_is_ignored(self, sent_message) -> None:
        """A send from WhatsApp Manager on the same number is not ours to record."""
        result = process_event(status_event("delivered", wamid="wamid.SOMEONE_ELSE"))

        sent_message.refresh_from_db()
        assert result.statuses_unmatched == 1
        assert sent_message.status == MessageStatus.SENT

    def test_a_failure_records_the_provider_error(self, sent_message) -> None:
        process_event(
            status_event(
                "failed",
                errors=[
                    {
                        "code": 131026,
                        "title": "Message undeliverable",
                        "error_data": {"details": "Receiver is not a WhatsApp user"},
                    }
                ],
            )
        )

        sent_message.refresh_from_db()
        assert sent_message.status == MessageStatus.FAILED
        assert sent_message.error_code == "131026"
        assert sent_message.error_message == "Receiver is not a WhatsApp user"

    def test_a_failure_is_surfaced_on_the_contact(self, sent_message) -> None:
        """So an operator can see why one number keeps failing."""
        process_event(
            status_event("failed", errors=[{"code": 131026, "title": "Message undeliverable"}])
        )

        sent_message.contact.refresh_from_db()
        assert sent_message.contact.last_error_code == "131026"

    def test_the_campaign_completes_when_its_last_report_lands(self, sent_message) -> None:
        """
        A campaign is finished when the provider says so, not when the last
        send request returned — otherwise it sits at "processing" forever.
        """
        assert sent_message.campaign.status == CampaignStatus.PROCESSING

        process_event(status_event("delivered"))

        sent_message.campaign.refresh_from_db()
        assert sent_message.campaign.status == CampaignStatus.COMPLETED

    def test_a_batch_of_statuses_is_applied_together(self, make_campaign, make_contact) -> None:
        campaign = make_campaign("Batch", status=CampaignStatus.PROCESSING)
        for index in range(3):
            contact = make_contact(f"C{index}", opted_in=True)
            Message.objects.create(
                campaign=campaign,
                contact=contact,
                to_phone_number=contact.phone_number,
                status=MessageStatus.SENT,
                provider_message_id=f"wamid.{index}",
            )

        event = WebhookEvent(
            payload={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "statuses": [
                                        {"id": f"wamid.{i}", "status": "delivered", "timestamp": "1749416383"}
                                        for i in range(3)
                                    ]
                                },
                                "field": "messages",
                            }
                        ]
                    }
                ]
            }
        )

        assert process_event(event).statuses_applied == 3


class TestStopKeyword:
    @pytest.mark.parametrize(
        "text", ["STOP", "stop", " Stop ", "STOP.", "unsubscribe", "Cancel", "QUIT", "opt out"]
    )
    def test_a_stop_request_is_recognised(self, text: str) -> None:
        assert is_stop_request(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "please don't stop sending these",
            "when does the sale stop?",
            "I want to stop by the shop",
            "stopwatch",
            "",
            "yes please",
        ],
    )
    def test_ordinary_messages_are_not_stop_requests(self, text: str) -> None:
        """
        Matched against the whole message, never searched for inside it. A
        false positive silently ends a conversation the customer wanted.
        """
        assert is_stop_request(text) is False


class TestInboundStop:
    def test_stop_opts_the_contact_out(self, sent_message) -> None:
        contact = sent_message.contact
        assert contact.opted_in is True

        result = process_event(inbound_event("STOP"))

        contact.refresh_from_db()
        assert result.opt_outs == 1
        assert contact.opted_in is False

    def test_the_opt_out_records_that_it_came_from_an_inbound_message(
        self, sent_message
    ) -> None:
        process_event(inbound_event("STOP"))

        sent_message.contact.refresh_from_db()
        assert sent_message.contact.opt_out_source == OptOutSource.INBOUND_STOP
        assert sent_message.contact.opt_out_at is not None

    def test_the_opt_out_is_audited_like_any_other(self, sent_message) -> None:
        """
        It goes through contacts.services.set_consent, so the compliance trail
        cannot tell the difference between this and an operator's action.
        """
        process_event(inbound_event("STOP"))

        assert AuditLog.objects.filter(action=AuditAction.CONTACT_OPTED_OUT).count() == 1

    def test_the_sender_is_matched_despite_the_missing_plus(self, sent_message) -> None:
        """Meta reports the sender without a "+"; we store E.164."""
        process_event(inbound_event("STOP", sender="9779800000001"))

        sent_message.contact.refresh_from_db()
        assert sent_message.contact.opted_in is False

    def test_an_ordinary_reply_changes_nothing(self, sent_message) -> None:
        result = process_event(inbound_event("Do you have this in blue?"))

        sent_message.contact.refresh_from_db()
        assert result.messages_received == 1
        assert result.opt_outs == 0
        assert sent_message.contact.opted_in is True

    def test_stop_from_an_unknown_number_is_harmless(self, sent_message) -> None:
        result = process_event(inbound_event("STOP", sender="9779899999999"))

        assert result.opt_outs == 0
        assert AuditLog.objects.filter(action=AuditAction.CONTACT_OPTED_OUT).count() == 0

    def test_stop_from_someone_already_opted_out_is_not_re_recorded(
        self, sent_message, make_contact
    ) -> None:
        process_event(inbound_event("STOP"))
        AuditLog.objects.all().delete()

        result = process_event(inbound_event("STOP"))

        assert result.opt_outs == 0
        assert AuditLog.objects.filter(action=AuditAction.CONTACT_OPTED_OUT).count() == 0

    def test_start_does_not_opt_anyone_in(self, make_contact) -> None:
        """
        Consent is never inferred. A keyword is a weaker basis than this system
        is willing to record, so opting back in stays a deliberate act with a
        source and an audit entry behind it.
        """
        contact = make_contact("Opted out", "+9779800000002", opted_in=False)

        process_event(inbound_event("START", sender="9779800000002"))

        contact.refresh_from_db()
        assert contact.opted_in is False


class TestResultSummary:
    def test_an_empty_payload_reports_nothing_rather_than_failing(self) -> None:
        assert process_event(WebhookEvent(payload={})) == ProcessResult()

    def test_statuses_and_messages_are_counted_separately(self, sent_message) -> None:
        event = WebhookEvent(
            payload={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "statuses": [
                                        {"id": "wamid.TEST123", "status": "read", "timestamp": "1749416383"}
                                    ],
                                    "messages": [
                                        {
                                            "from": "9779800000001",
                                            "id": "wamid.IN",
                                            "type": "text",
                                            "text": {"body": "STOP"},
                                        }
                                    ],
                                },
                                "field": "messages",
                            }
                        ]
                    }
                ]
            }
        )

        result = process_event(event)

        assert result.statuses_applied == 1
        assert result.messages_received == 1
        assert result.opt_outs == 1
