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

    def test_the_activity_chart_is_rendered(self, auth_client: Client) -> None:
        context = auth_client.get(reverse("dashboard:home")).context
        assert context["chart_period"].days == 14
        assert len(context["activity"]) == 14

    def test_an_empty_chart_says_so_rather_than_drawing_a_flat_line(
        self, auth_client: Client
    ) -> None:
        """A flat line at zero claims nothing happened; an absent chart admits it."""
        response = auth_client.get(reverse("dashboard:home"))

        assert response.context["chart"].has_data is False
        assert "Nothing sent in this period" in response.content.decode()

    def test_the_chart_numbers_are_readable_without_the_colours(
        self, auth_client: Client, launched_campaign, make_message
    ) -> None:
        from messaging.models import MessageStatus

        make_message(launched_campaign(), status=MessageStatus.DELIVERED)

        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert "Show the numbers" in body

    def test_campaigns_that_are_sending_are_listed(
        self, auth_client: Client, launched_campaign, make_message
    ) -> None:
        from campaigns.models import CampaignStatus
        from messaging.models import MessageStatus

        campaign = launched_campaign("Live one", status=CampaignStatus.PROCESSING)
        make_message(campaign, status=MessageStatus.SENT)

        response = auth_client.get(reverse("dashboard:home"))

        assert [row["row"].name for row in response.context["active_campaigns"]] == ["Live one"]
        assert "Sending now" in response.content.decode()

    def test_nothing_sending_hides_the_live_panel(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("dashboard:home")).content.decode()
        assert "Sending now" not in body

    def test_recent_failures_name_the_reason(
        self, auth_client: Client, launched_campaign, make_message
    ) -> None:
        from messaging.models import MessageStatus

        make_message(
            launched_campaign(),
            status=MessageStatus.FAILED,
            error_code="131026",
            error_message="Message undeliverable",
        )

        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert "Message undeliverable" in body
        assert "131026" in body


class TestReportsView:
    def test_anonymous_visitors_are_redirected_to_login(self, client: Client) -> None:
        response = client.get(reverse("dashboard:reports"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_a_viewer_may_read_reports(self, viewer) -> None:
        client = Client()
        client.force_login(viewer)
        assert client.get(reverse("dashboard:reports")).status_code == 200

    def test_the_default_period_is_the_last_thirty_days(self, auth_client: Client) -> None:
        from dashboard import services

        period = auth_client.get(reverse("dashboard:reports")).context["period"]
        assert period.days == services.DEFAULT_PERIOD_DAYS

    def test_a_preset_scopes_the_whole_page(self, auth_client: Client) -> None:
        context = auth_client.get(reverse("dashboard:reports"), {"days": 7}).context

        assert context["period"].days == 7
        assert context["selected_preset"] == 7
        assert len(context["activity"]) == 7

    def test_a_custom_range_scopes_the_whole_page(self, auth_client: Client) -> None:
        context = auth_client.get(
            reverse("dashboard:reports"), {"start": "2026-08-01", "end": "2026-08-10"}
        ).context

        assert context["period"].days == 10
        # No preset matches a hand-picked range, so none is highlighted.
        assert context["selected_preset"] is None

    def test_a_hand_edited_period_does_not_break_the_page(self, auth_client: Client) -> None:
        assert auth_client.get(reverse("dashboard:reports"), {"days": "🙂"}).status_code == 200

    def test_launched_campaigns_are_tabulated(
        self, auth_client: Client, launched_campaign, make_message
    ) -> None:
        from messaging.models import MessageStatus

        campaign = launched_campaign("Summer Sale")
        make_message(campaign, status=MessageStatus.DELIVERED)

        response = auth_client.get(reverse("dashboard:reports"))

        assert [entry["row"].name for entry in response.context["campaign_rows"]] == ["Summer Sale"]
        assert "Summer Sale" in response.content.decode()

    def test_drafts_are_explained_rather_than_silently_missing(
        self, auth_client: Client, make_campaign
    ) -> None:
        make_campaign("Never launched")

        body = auth_client.get(reverse("dashboard:reports")).content.decode()

        assert "Never launched" not in body
        assert "No campaigns were launched in this period" in body

    def test_the_consent_panel_says_it_ignores_the_period(self, auth_client: Client) -> None:
        """Otherwise a reader would take it for a figure about the window."""
        body = auth_client.get(reverse("dashboard:reports"), {"days": 7}).content.decode()
        assert "not the selected period" in body

    def test_downloads_carry_the_selected_period(self, auth_client: Client) -> None:
        response = auth_client.get(
            reverse("dashboard:reports"), {"start": "2026-08-01", "end": "2026-08-10"}
        )

        assert response.context["period_query"] == "start=2026-08-01&end=2026-08-10"
        assert "start=2026-08-01&amp;end=2026-08-10" in response.content.decode()

    def test_every_catalogued_report_is_offered(self, auth_client: Client) -> None:
        from dashboard import reports

        body = auth_client.get(reverse("dashboard:reports")).content.decode()

        for spec in reports.REPORTS.values():
            assert spec.label in body, spec.slug

    def test_the_timezone_the_figures_use_is_stated(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("dashboard:reports")).content.decode()
        assert "Times are shown in" in body

    def test_no_credential_is_rendered_into_the_page(
        self, auth_client: Client, settings
    ) -> None:
        settings.META_ACCESS_TOKEN = "EAAtopsecrettoken1234567890"
        settings.META_APP_SECRET = "topsecretappsecret"

        body = auth_client.get(reverse("dashboard:reports")).content.decode()

        assert "EAAtopsecrettoken1234567890" not in body
        assert "topsecretappsecret" not in body

    def test_the_reports_page_is_reachable_from_the_navigation(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("dashboard:home")).content.decode()
        assert reverse("dashboard:reports") in body
