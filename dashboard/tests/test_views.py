"""The dashboard must never be reachable without an active session."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestHomeView:
    def test_anonymous_visitors_are_redirected_to_login(self, client: Client) -> None:
        response = client.get(reverse("dashboard:home"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_signed_in_user_sees_the_dashboard(self, auth_client: Client) -> None:
        response = auth_client.get(reverse("dashboard:home"))
        assert response.status_code == 200
        assert "Dashboard" in response.content.decode()

    def test_deactivated_user_is_signed_out(self, auth_client: Client, operator) -> None:
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        response = auth_client.get(reverse("dashboard:home"))

        assert response.status_code == 302

    def test_mock_provider_is_flagged_in_the_ui(self, auth_client: Client) -> None:
        """An operator must never mistake a simulated send for a real one."""
        response = auth_client.get(reverse("dashboard:home"))
        assert "Mock provider active" in response.content.decode()

    def test_no_credential_is_rendered_into_the_page(self, auth_client: Client, settings) -> None:
        settings.META_ACCESS_TOKEN = "EAAtopsecrettoken1234567890"
        settings.META_APP_SECRET = "topsecretappsecret"

        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert "EAAtopsecrettoken1234567890" not in body
        assert "topsecretappsecret" not in body

    def test_all_modules_report_as_installed(self, auth_client: Client) -> None:
        modules = auth_client.get(reverse("dashboard:home")).context["modules"]
        assert all(modules.values()), modules

    def test_contact_tiles_show_real_counts(self, auth_client: Client, make_contact) -> None:
        make_contact("Consenting", opted_in=True)
        make_contact("Not consenting", opted_in=False)

        stats = auth_client.get(reverse("dashboard:home")).context["contact_stats"]

        assert stats["total"] == 2
        assert stats["opted_in"] == 1
        assert stats["eligible"] == 1

    def test_campaign_and_message_tiles_show_real_counts(
        self, auth_client: Client, make_campaign, make_contact
    ) -> None:
        from messaging.models import Message, MessageStatus

        campaign = make_campaign("Summer")
        Message.objects.create(
            campaign=campaign,
            contact=make_contact("A"),
            to_phone_number="+9779800000001",
            status=MessageStatus.DELIVERED,
        )
        Message.objects.create(
            campaign=campaign,
            contact=make_contact("B"),
            to_phone_number="+9779800000002",
            status=MessageStatus.FAILED,
        )

        context = auth_client.get(reverse("dashboard:home")).context

        assert context["campaign_stats"]["total"] == 1
        assert context["message_stats"]["delivered"] == 1
        assert context["message_stats"]["failed"] == 1

    def test_recent_campaigns_are_listed(self, auth_client: Client, make_campaign) -> None:
        make_campaign("Summer Sale")
        body = auth_client.get(reverse("dashboard:home")).content.decode()
        assert "Summer Sale" in body

    def test_sender_status_is_reported_honestly(self, auth_client: Client) -> None:
        """The dashboard must not imply sending works when nothing can send."""
        status = auth_client.get(reverse("dashboard:home")).context["system_status"]

        assert status["dispatcher_registered"] is False
        assert status["can_send"] is False

    def test_can_send_requires_both_a_dispatcher_and_a_reachable_broker(
        self, auth_client: Client, recording_dispatcher
    ) -> None:
        status = auth_client.get(reverse("dashboard:home")).context["system_status"]

        assert status["dispatcher_registered"] is True
        # Eager mode reports the broker as reachable: tasks run inline.
        assert status["broker_reachable"] is True
        assert status["can_send"] is True

    def test_an_unreachable_broker_blocks_sending_even_with_a_dispatcher(
        self, auth_client: Client, recording_dispatcher
    ) -> None:
        """A registered sender is not the same as a working queue."""
        from unittest.mock import patch

        from whatsapp.health import BrokerHealth

        with patch(
            "whatsapp.health.check_broker",
            return_value=BrokerHealth(False, "Could not connect."),
        ):
            status = auth_client.get(reverse("dashboard:home")).context["system_status"]

        assert status["dispatcher_registered"] is True
        assert status["broker_reachable"] is False
        assert status["can_send"] is False

    def test_provider_and_rate_ceiling_are_reported(self, auth_client: Client) -> None:
        status = auth_client.get(reverse("dashboard:home")).context["system_status"]
        assert status["provider"] == "mock"
        assert status["is_simulated"] is True
