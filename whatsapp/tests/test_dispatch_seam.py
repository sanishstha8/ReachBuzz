"""
The seam between the campaign services and the Celery sender.

Phase 4 built the plan; Phase 5 supplies the worker. The point of the seam is
that campaign services never import Celery, and that a launch is refused before
any state change when the queue cannot actually accept the work.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from campaigns import dispatch
from campaigns.dispatch import SendingUnavailable
from campaigns.models import CampaignStatus
from campaigns.services import launch_campaign
from messaging.models import Message

pytestmark = pytest.mark.django_db


class TestRegistration:
    def test_the_app_registers_the_celery_sender_at_startup(self) -> None:
        """WhatsappConfig.ready() wires the two apps together."""
        from django.apps import apps

        from whatsapp import tasks

        apps.get_app_config("whatsapp").ready()
        try:
            assert dispatch.get_dispatcher() is tasks.queue_campaign
            assert dispatch.is_sending_available() is True
        finally:
            dispatch.clear_dispatcher()

    def test_campaign_services_do_not_import_celery(self) -> None:
        """The seam exists so campaign logic stays testable without a broker."""
        import inspect

        from campaigns import services

        source = inspect.getsource(services)
        assert "celery" not in source.lower()
        assert "import whatsapp.tasks" not in source


class TestPreflight:
    def test_no_dispatcher_means_no_launch(self, ready_campaign) -> None:
        with pytest.raises(SendingUnavailable):
            launch_campaign(ready_campaign)

        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.DRAFT
        assert Message.objects.count() == 0

    def test_the_celery_dispatcher_advertises_its_preflight(self) -> None:
        """
        The hook is bound as an attribute on the dispatcher itself, which is how
        ``campaigns.dispatch.preflight()`` finds it without importing Celery.
        """
        from whatsapp import tasks

        assert tasks.queue_campaign.preflight is tasks.preflight

    def test_an_unreachable_broker_blocks_the_launch(self, ready_campaign) -> None:
        """
        A registered dispatcher is not a reachable queue. Without this check the
        campaign would go to PROCESSING and every message would stall at PENDING
        with nothing able to move it.
        """

        def failing_dispatcher(campaign) -> int:  # pragma: no cover - never reached
            raise AssertionError("dispatch must not be attempted")

        def failing_preflight() -> None:
            raise SendingUnavailable(
                "The message queue is unreachable, so this campaign cannot be sent yet."
            )

        failing_dispatcher.preflight = failing_preflight
        dispatch.register_dispatcher(failing_dispatcher)

        try:
            with pytest.raises(SendingUnavailable, match="unreachable"):
                launch_campaign(ready_campaign)
        finally:
            dispatch.clear_dispatcher()

        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.DRAFT
        assert Message.objects.count() == 0

    def test_preflight_is_skipped_in_eager_mode(self, celery_dispatcher) -> None:
        from whatsapp.tasks import preflight

        # CELERY_TASK_ALWAYS_EAGER is on in the test settings: no broker needed.
        preflight()

    def test_preflight_raises_when_the_broker_cannot_be_reached(self) -> None:
        """
        Patch the connection rather than pointing at a dead port: this asserts
        the behaviour deterministically and without waiting for a TCP timeout.
        """
        from whatsapp.tasks import preflight

        broken = patch(
            "config.celery.app.connection",
            side_effect=OSError("Connection refused"),
        )

        with override_settings(CELERY_TASK_ALWAYS_EAGER=False), broken:
            with pytest.raises(SendingUnavailable, match="queue is unreachable"):
                preflight()

    def test_preflight_passes_when_the_broker_answers(self) -> None:
        from unittest.mock import MagicMock

        from whatsapp.tasks import preflight

        connection = MagicMock()
        with override_settings(CELERY_TASK_ALWAYS_EAGER=False):
            with patch("config.celery.app.connection", return_value=connection):
                preflight()

        connection.ensure_connection.assert_called_once()
        connection.release.assert_called_once()

    def test_a_dispatcher_without_preflight_is_accepted(self, recording_dispatcher) -> None:
        """The hook is optional; a plain callable is still a valid dispatcher."""
        assert not hasattr(recording_dispatcher, "preflight")
        dispatch.preflight()


class TestQueueCampaign:
    def test_returns_the_number_of_queued_messages(
        self, ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            launch_campaign(ready_campaign)

        assert Message.objects.filter(campaign=ready_campaign).count() == 3
