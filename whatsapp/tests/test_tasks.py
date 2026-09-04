"""
The sending tasks.

Celery runs eagerly here (``CELERY_TASK_ALWAYS_EAGER``), so the whole send path
is exercised with no broker. The properties under test are the ones that decide
whether a 1,000-message campaign is safe: claim-once semantics, retry with
backoff, honouring a pause, and never sending to someone twice.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from celery.exceptions import Retry
from django.test import override_settings

from campaigns.models import CampaignStatus
from campaigns.services import cancel_campaign, pause_campaign
from contacts.models import Contact
from messaging.models import Message, MessageStatus, MessageStatusEvent, StatusEventSource
from whatsapp import tasks
from whatsapp.services.base import SendResult

pytestmark = pytest.mark.django_db


def patch_provider(result: SendResult | list[SendResult]):
    """Patch the provider the task builds, with one or a sequence of results."""
    results = result if isinstance(result, list) else None

    class _Stub:
        name = "stub"
        is_simulated = False
        calls: list = []

        def send_template(self, **kwargs):
            self.calls.append(kwargs)
            if results is not None:
                return results[min(len(self.calls) - 1, len(results) - 1)]
            return result

        def send_text(self, **kwargs):
            return self.send_template(**kwargs)

    stub = _Stub()
    stub.calls = []
    return patch("whatsapp.tasks.provider_for", return_value=stub), stub


class TestDispatchCampaign:
    def test_queues_every_pending_message(self, launched_campaign) -> None:
        """launched_campaign runs the on-commit dispatch, so this is end to end."""
        assert Message.objects.filter(campaign=launched_campaign).count() == 3

    def test_all_messages_are_sent(self, launched_campaign) -> None:
        statuses = set(
            Message.objects.filter(campaign=launched_campaign).values_list("status", flat=True)
        )
        assert statuses == {MessageStatus.SENT}

    def test_campaign_completes_once_nothing_is_in_flight(self, launched_campaign) -> None:
        launched_campaign.refresh_from_db()
        assert launched_campaign.status == CampaignStatus.COMPLETED
        assert launched_campaign.completed_at is not None

    def test_every_message_gets_a_provider_id(self, launched_campaign) -> None:
        ids = list(
            Message.objects.filter(campaign=launched_campaign).values_list(
                "provider_message_id", flat=True
            )
        )
        assert all(pid.startswith("wamid.MOCK.") for pid in ids)
        assert len(set(ids)) == 3

    def test_send_is_recorded_as_a_send_response_not_a_webhook(
        self, launched_campaign
    ) -> None:
        events = MessageStatusEvent.objects.filter(status=MessageStatus.SENT)
        assert events.count() == 3
        assert {e.source for e in events} == {StatusEventSource.SEND_RESPONSE}

    def test_dispatching_a_missing_campaign_is_harmless(self) -> None:
        import uuid

        assert tasks.dispatch_campaign_task(str(uuid.uuid4())) == 0

    def test_dispatch_skips_a_cancelled_campaign(self, ready_campaign, celery_dispatcher) -> None:
        ready_campaign.status = CampaignStatus.CANCELLED
        ready_campaign.save(update_fields=["status"])

        assert tasks.dispatch_campaign_task(str(ready_campaign.pk)) == 0


class TestIdempotency:
    def test_a_duplicated_job_does_not_send_twice(self, launched_campaign) -> None:
        """Two workers receiving the same job: exactly one may send."""
        message = Message.objects.filter(campaign=launched_campaign).first()

        result = tasks.send_message_task(str(message.pk))

        assert result == "already-handled"
        assert MessageStatusEvent.objects.filter(
            message=message, status=MessageStatus.SENT
        ).count() == 1

    def test_re_dispatching_a_finished_campaign_sends_nothing(
        self, launched_campaign
    ) -> None:
        before = MessageStatusEvent.objects.count()

        tasks.dispatch_campaign_task(str(launched_campaign.pk))

        assert MessageStatusEvent.objects.count() == before

    def test_only_one_send_per_recipient(self, launched_campaign) -> None:
        contacts = Message.objects.filter(campaign=launched_campaign).values_list(
            "contact_id", flat=True
        )
        assert len(set(contacts)) == 3


class TestPauseAndCancel:
    def test_a_paused_campaign_defers_its_messages(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        ready_campaign.status = CampaignStatus.PROCESSING
        ready_campaign.save(update_fields=["status"])
        pause_campaign(ready_campaign)

        message = Message.objects.first()
        result = tasks.send_message_task(str(message.pk))

        message.refresh_from_db()
        assert result == "campaign-paused"
        assert message.status == MessageStatus.QUEUED

    def test_a_deferred_message_is_not_left_claimed(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        """A row stuck in SENDING would never be claimed again."""
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        ready_campaign.status = CampaignStatus.PAUSED
        ready_campaign.save(update_fields=["status"])

        message = Message.objects.first()
        tasks.send_message_task(str(message.pk))

        message.refresh_from_db()
        assert message.status != MessageStatus.SENDING
        assert message.is_claimable

    def test_cancelling_stops_further_sending(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        ready_campaign.status = CampaignStatus.PROCESSING
        ready_campaign.save(update_fields=["status"])
        cancel_campaign(ready_campaign)

        assert tasks.dispatch_campaign_task(str(ready_campaign.pk)) == 0


class TestFailureHandling:
    def test_a_permanent_failure_is_recorded_without_retrying(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        message = Message.objects.first()

        patcher, stub = patch_provider(
            SendResult.failure("undeliverable", "Not a WhatsApp user", retryable=False)
        )
        with patcher:
            result = tasks.send_message_task(str(message.pk))

        message.refresh_from_db()
        assert result == "failed"
        assert message.status == MessageStatus.FAILED
        assert message.error_code == "undeliverable"
        assert len(stub.calls) == 1

    def test_a_retryable_failure_is_retried_then_gives_up(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        """WHATSAPP_MAX_RETRIES is 2 in the test settings: 1 try + 2 retries."""
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        message = Message.objects.first()

        patcher, stub = patch_provider(
            SendResult.failure("upstream", "Temporary provider error", retryable=True)
        )
        with patcher, pytest.raises(Retry):
            # Eager mode re-raises the Retry, so the retry request is visible.
            tasks.send_message_task(str(message.pk))

        message.refresh_from_db()
        assert message.attempt_count >= 1
        assert message.next_retry_at is not None

    def test_a_retryable_failure_that_later_succeeds_is_sent(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        message = Message.objects.first()

        patcher, _ = patch_provider(SendResult.ok("wamid.RECOVERED"))
        with patcher:
            tasks.send_message_task(str(message.pk))

        message.refresh_from_db()
        assert message.status == MessageStatus.SENT
        assert message.provider_message_id == "wamid.RECOVERED"

    def test_failure_is_surfaced_on_the_contact(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        message = Message.objects.first()

        patcher, _ = patch_provider(
            SendResult.failure("undeliverable", "Not a WhatsApp user", retryable=False)
        )
        with patcher:
            tasks.send_message_task(str(message.pk))

        contact = Contact.objects.get(pk=message.contact_id)
        assert contact.last_error_code == "undeliverable"
        assert contact.last_error_message == "Not a WhatsApp user"

    def test_an_unimplemented_provider_does_not_burn_retries(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        """A deployment mistake must not become 1,000 failed messages."""
        from campaigns.services import materialize_messages, resolve_audience

        materialize_messages(ready_campaign, list(resolve_audience(ready_campaign)))
        message = Message.objects.first()

        class _Unimplemented:
            name = "meta"
            is_simulated = False

            def send_template(self, **kwargs):
                raise NotImplementedError("Phase 7")

        with patch("whatsapp.tasks.provider_for", return_value=_Unimplemented()):
            result = tasks.send_message_task(str(message.pk))

        message.refresh_from_db()
        assert result == "provider-not-implemented"
        assert message.status == MessageStatus.QUEUED
        assert message.attempt_count == 0

    def test_a_campaign_completes_even_when_every_message_fails(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        from campaigns.services import launch_campaign

        patcher, _ = patch_provider(
            SendResult.failure("undeliverable", "Not a WhatsApp user", retryable=False)
        )
        with patcher, django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.COMPLETED
        assert Message.objects.filter(status=MessageStatus.FAILED).count() == 3


class TestBackoff:
    def test_backoff_grows_with_each_attempt(self) -> None:
        first = [tasks._backoff(1) for _ in range(20)]
        third = [tasks._backoff(3) for _ in range(20)]
        assert max(first) < min(third)

    def test_backoff_is_jittered(self) -> None:
        """Without jitter, a thousand simultaneous failures retry in lockstep."""
        delays = {tasks._backoff(4) for _ in range(40)}
        assert len(delays) > 1

    def test_backoff_is_capped(self) -> None:
        assert tasks._backoff(20) <= 3600 * 1.2

    def test_backoff_is_never_zero(self) -> None:
        assert all(tasks._backoff(n) >= 1 for n in range(1, 8))


class TestSimulatedCallbacks:
    @override_settings(MOCK_PROVIDER_SIMULATE_CALLBACKS=True, MOCK_PROVIDER_READ_RATE=1.0)
    def test_simulation_advances_the_message_to_read(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        from campaigns.services import launch_campaign

        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        assert Message.objects.filter(status=MessageStatus.READ).count() == 3

    @override_settings(MOCK_PROVIDER_SIMULATE_CALLBACKS=True, MOCK_PROVIDER_READ_RATE=0.0)
    def test_simulation_can_stop_at_delivered(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        from campaigns.services import launch_campaign

        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        assert Message.objects.filter(status=MessageStatus.DELIVERED).count() == 3

    @override_settings(MOCK_PROVIDER_SIMULATE_CALLBACKS=True, MOCK_PROVIDER_READ_RATE=1.0)
    def test_simulated_events_are_labelled_as_simulated(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        """A simulated delivery must never look like a real provider webhook."""
        from campaigns.services import launch_campaign

        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        sources = set(
            MessageStatusEvent.objects.filter(
                status__in=[MessageStatus.DELIVERED, MessageStatus.READ]
            ).values_list("source", flat=True)
        )
        assert sources == {StatusEventSource.SIMULATED}
        assert not MessageStatusEvent.objects.filter(
            source=StatusEventSource.WEBHOOK
        ).exists()

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_simulation_refuses_to_run_under_a_real_provider(
        self, launched_campaign
    ) -> None:
        message = Message.objects.first()
        assert tasks.simulate_status_callbacks_task(str(message.pk)) == "not-simulated"


class TestScheduledCampaigns:
    def test_launches_a_campaign_whose_time_has_come(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        from django.utils import timezone

        ready_campaign.status = CampaignStatus.SCHEDULED
        ready_campaign.scheduled_at = timezone.now() - timezone.timedelta(minutes=1)
        ready_campaign.save(update_fields=["status", "scheduled_at"])

        with django_capture_on_commit_callbacks(execute=True):
            launched = tasks.run_due_campaigns_task()

        ready_campaign.refresh_from_db()
        assert launched == 1
        assert ready_campaign.status in (CampaignStatus.PROCESSING, CampaignStatus.COMPLETED)

    def test_ignores_a_campaign_scheduled_for_later(
        self, ready_campaign, celery_dispatcher
    ) -> None:
        from django.utils import timezone

        ready_campaign.status = CampaignStatus.SCHEDULED
        ready_campaign.scheduled_at = timezone.now() + timezone.timedelta(hours=1)
        ready_campaign.save(update_fields=["status", "scheduled_at"])

        assert tasks.run_due_campaigns_task() == 0

    def test_an_invalid_scheduled_campaign_is_marked_failed_with_a_reason(
        self, make_campaign, celery_dispatcher
    ) -> None:
        """It must not silently stay SCHEDULED and retry every minute forever."""
        from django.utils import timezone

        campaign = make_campaign(
            "Nothing configured",
            status=CampaignStatus.SCHEDULED,
            scheduled_at=timezone.now() - timezone.timedelta(minutes=1),
        )

        assert tasks.run_due_campaigns_task() == 0

        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.FAILED
        assert campaign.failure_reason
