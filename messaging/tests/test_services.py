"""
Message status handling.

Two properties matter here, because Phase 7's webhook handler depends on both:
status updates must be **idempotent** (a redelivered callback changes nothing
twice) and **monotonic** (a late "sent" never overwrites "read").
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from campaigns.models import CampaignMessageType
from messaging.models import Message, MessageStatus, MessageStatusEvent, StatusEventSource
from messaging.services import (
    StatusUpdate,
    apply_status_update,
    campaign_stats,
    claim_for_sending,
    global_stats,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def message(make_campaign, make_contact):
    contact = make_contact("Aarav", opted_in=True)
    return Message.objects.create(
        campaign=make_campaign(),
        contact=contact,
        to_phone_number=contact.phone_number,
        message_type=CampaignMessageType.TEMPLATE,
        rendered_payload={"text": "Hello Aarav"},
    )


def update(status: str, **kwargs) -> StatusUpdate:
    payload = StatusUpdate(status=status, **kwargs)
    payload.provider_timestamp = kwargs.get("provider_timestamp") or timezone.now()
    return payload


class TestStatusProgression:
    def test_advances_through_the_normal_lifecycle(self, message) -> None:
        for status in (
            MessageStatus.SENT,
            MessageStatus.DELIVERED,
            MessageStatus.READ,
        ):
            assert apply_status_update(message, update(status)) is True
            assert message.status == status

    def test_stamps_the_matching_timestamp(self, message) -> None:
        apply_status_update(message, update(MessageStatus.SENT))
        apply_status_update(message, update(MessageStatus.DELIVERED))

        message.refresh_from_db()
        assert message.sent_at is not None
        assert message.delivered_at is not None
        assert message.read_at is None

    def test_records_the_provider_message_id(self, message) -> None:
        apply_status_update(message, update(MessageStatus.SENT, provider_message_id="wamid.ABC"))

        message.refresh_from_db()
        assert message.provider_message_id == "wamid.ABC"

    def test_every_update_appends_an_event(self, message) -> None:
        apply_status_update(message, update(MessageStatus.SENT))
        apply_status_update(message, update(MessageStatus.DELIVERED))

        assert MessageStatusEvent.objects.filter(message=message).count() == 2


class TestMonotonicity:
    def test_a_late_sent_does_not_overwrite_read(self, message) -> None:
        """Callbacks arrive out of order; the message must not go backwards."""
        apply_status_update(message, update(MessageStatus.READ))

        applied = apply_status_update(
            message, update(MessageStatus.SENT, provider_timestamp=timezone.now())
        )

        assert applied is False
        message.refresh_from_db()
        assert message.status == MessageStatus.READ

    def test_the_out_of_order_event_is_still_recorded(self, message) -> None:
        """The status does not move, but the history keeps the full record."""
        apply_status_update(message, update(MessageStatus.READ))
        apply_status_update(message, update(MessageStatus.SENT))

        statuses = set(
            MessageStatusEvent.objects.filter(message=message).values_list("status", flat=True)
        )
        assert statuses == {MessageStatus.READ, MessageStatus.SENT}

    def test_delivered_does_not_overwrite_read(self, message) -> None:
        apply_status_update(message, update(MessageStatus.READ))
        apply_status_update(message, update(MessageStatus.DELIVERED))

        message.refresh_from_db()
        assert message.status == MessageStatus.READ

    def test_outranks_helper(self, message) -> None:
        message.status = MessageStatus.DELIVERED
        assert message.outranks(MessageStatus.SENT) is True
        assert message.outranks(MessageStatus.DELIVERED) is True
        assert message.outranks(MessageStatus.READ) is False


class TestIdempotency:
    def test_a_redelivered_event_changes_nothing(self, message) -> None:
        stamp = timezone.now()
        first = apply_status_update(
            message, update(MessageStatus.SENT, provider_timestamp=stamp)
        )
        second = apply_status_update(
            message, update(MessageStatus.SENT, provider_timestamp=stamp)
        )

        assert first is True
        assert second is False
        assert MessageStatusEvent.objects.filter(message=message).count() == 1

    def test_the_same_status_at_a_different_time_is_a_new_event(self, message) -> None:
        now = timezone.now()
        apply_status_update(message, update(MessageStatus.SENT, provider_timestamp=now))
        apply_status_update(
            message, update(MessageStatus.SENT, provider_timestamp=now + timedelta(seconds=5))
        )

        assert MessageStatusEvent.objects.filter(message=message).count() == 2


class TestFailure:
    def test_failure_records_the_error(self, message) -> None:
        apply_status_update(
            message,
            update(
                MessageStatus.FAILED,
                error_code="131026",
                error_message="Message undeliverable",
                payload={"detail": "not a WhatsApp user"},
            ),
        )

        message.refresh_from_db()
        assert message.status == MessageStatus.FAILED
        assert message.error_code == "131026"
        assert message.error_message == "Message undeliverable"
        assert message.failed_at is not None

    def test_long_error_text_is_truncated_rather_than_raising(self, message) -> None:
        apply_status_update(
            message, update(MessageStatus.FAILED, error_message="x" * 400, error_code="y" * 60)
        )

        message.refresh_from_db()
        assert len(message.error_message) == 255
        assert len(message.error_code) == 32


class TestClaiming:
    def test_claiming_a_pending_message_succeeds(self, message) -> None:
        claimed = claim_for_sending(message.pk)

        assert claimed is not None
        assert claimed.status == MessageStatus.SENDING

    def test_a_second_claim_returns_nothing(self, message) -> None:
        """Two workers on the same job: exactly one may send."""
        assert claim_for_sending(message.pk) is not None
        assert claim_for_sending(message.pk) is None

    def test_an_already_sent_message_cannot_be_claimed(self, message) -> None:
        message.status = MessageStatus.SENT
        message.save(update_fields=["status"])

        assert claim_for_sending(message.pk) is None

    def test_a_failed_message_cannot_be_claimed(self, message) -> None:
        message.status = MessageStatus.FAILED
        message.save(update_fields=["status"])

        assert claim_for_sending(message.pk) is None


class TestCampaignStats:
    def test_counts_each_status(self, make_campaign, make_contact) -> None:
        campaign = make_campaign()
        statuses = [
            MessageStatus.PENDING,
            MessageStatus.SENT,
            MessageStatus.DELIVERED,
            MessageStatus.READ,
            MessageStatus.FAILED,
        ]
        for index, status in enumerate(statuses):
            Message.objects.create(
                campaign=campaign,
                contact=make_contact(f"C{index}"),
                to_phone_number=f"+97798000000{index:02d}",
                status=status,
            )

        stats = campaign_stats(campaign)

        assert stats.total == 5
        assert stats.pending == 1
        assert stats.failed == 1
        assert stats.reached == 3
        assert stats.in_flight == 1
        assert stats.processed == 4

    def test_progress_percentage(self, make_campaign, make_contact) -> None:
        campaign = make_campaign()
        for index in range(4):
            Message.objects.create(
                campaign=campaign,
                contact=make_contact(f"C{index}"),
                to_phone_number=f"+97798100000{index:02d}",
                status=MessageStatus.SENT if index < 3 else MessageStatus.PENDING,
            )

        assert campaign_stats(campaign).progress_percent == 75.0

    def test_empty_campaign_reports_zero_not_a_division_error(self, make_campaign) -> None:
        stats = campaign_stats(make_campaign())
        assert stats.progress_percent == 0.0
        assert stats.delivery_rate == 0.0
        assert stats.failure_rate == 0.0

    def test_delivery_rate_uses_accepted_messages_as_the_base(
        self, make_campaign, make_contact
    ) -> None:
        campaign = make_campaign()
        for index, status in enumerate(
            [MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.FAILED]
        ):
            Message.objects.create(
                campaign=campaign,
                contact=make_contact(f"C{index}"),
                to_phone_number=f"+97798200000{index:02d}",
                status=status,
            )

        # 1 delivered out of 2 the provider accepted; the failure is not in the base.
        assert campaign_stats(campaign).delivery_rate == 50.0


class TestGlobalStats:
    def test_aggregates_across_campaigns(self, make_campaign, make_contact) -> None:
        for index, status in enumerate(
            [MessageStatus.SENT, MessageStatus.READ, MessageStatus.FAILED, MessageStatus.PENDING]
        ):
            Message.objects.create(
                campaign=make_campaign(),
                contact=make_contact(f"C{index}"),
                to_phone_number=f"+97798300000{index:02d}",
                status=status,
            )

        stats = global_stats()

        assert stats["total"] == 4
        assert stats["sent"] == 2
        assert stats["read"] == 1
        assert stats["failed"] == 1
        assert stats["pending"] == 1


class TestEventSources:
    def test_source_defaults_to_webhook(self, message) -> None:
        apply_status_update(message, update(MessageStatus.SENT))
        assert MessageStatusEvent.objects.get().source == StatusEventSource.WEBHOOK

    def test_send_response_source_is_recorded(self, message) -> None:
        apply_status_update(
            message, update(MessageStatus.SENT, source=StatusEventSource.SEND_RESPONSE)
        )
        assert MessageStatusEvent.objects.get().source == StatusEventSource.SEND_RESPONSE


class TestFailureBreakdown:
    """
    Grouped failures live in messaging because they are a fact about messages:
    the campaign monitoring page and the reports page both ask for them, and a
    second implementation is how the two would come to disagree.
    """

    def test_identical_errors_are_grouped(self, make_campaign, make_contact) -> None:
        from messaging.services import failure_breakdown

        campaign = make_campaign()
        for index in range(3):
            Message.objects.create(
                campaign=campaign,
                contact=make_contact(f"Failed {index}"),
                to_phone_number=f"+97798400000{index:02d}",
                status=MessageStatus.FAILED,
                error_code="131026",
                error_message="Message undeliverable",
            )

        reasons = failure_breakdown(Message.objects.all())

        assert len(reasons) == 1
        assert reasons[0].count == 3
        assert reasons[0].label == "Message undeliverable"

    def test_the_commonest_error_comes_first(self, make_campaign, make_contact) -> None:
        from messaging.services import failure_breakdown

        campaign = make_campaign()
        for index, code in enumerate(["470", "131026", "131026"]):
            Message.objects.create(
                campaign=campaign,
                contact=make_contact(f"Failed {index}"),
                to_phone_number=f"+97798410000{index:02d}",
                status=MessageStatus.FAILED,
                error_code=code,
            )

        assert [r.error_code for r in failure_breakdown(Message.objects.all())] == ["131026", "470"]

    def test_only_failures_are_counted(self, make_campaign, make_contact) -> None:
        from messaging.services import failure_breakdown

        Message.objects.create(
            campaign=make_campaign(),
            contact=make_contact("Delivered"),
            to_phone_number="+9779842000001",
            status=MessageStatus.DELIVERED,
        )

        assert failure_breakdown(Message.objects.all()) == []

    def test_an_error_with_no_code_still_has_a_label(self, make_campaign, make_contact) -> None:
        from messaging.services import failure_breakdown

        Message.objects.create(
            campaign=make_campaign(),
            contact=make_contact("Failed"),
            to_phone_number="+9779843000001",
            status=MessageStatus.FAILED,
        )

        assert failure_breakdown(Message.objects.all())[0].label == "Unknown error"

    def test_campaign_scope_counts_only_that_campaign(self, make_campaign, make_contact) -> None:
        from messaging.services import campaign_failure_reasons

        wanted = make_campaign("Wanted")
        other = make_campaign("Other")
        for index, campaign in enumerate([wanted, other]):
            Message.objects.create(
                campaign=campaign,
                contact=make_contact(f"Failed {index}"),
                to_phone_number=f"+97798440000{index:02d}",
                status=MessageStatus.FAILED,
                error_code="470",
            )

        assert campaign_failure_reasons(wanted)[0].count == 1
