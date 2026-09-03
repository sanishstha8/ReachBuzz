"""
The reporting API.

The point of these endpoints is that they are the *same* figures as the HTML
pages, not a second implementation, so several tests here assert the JSON
against the service layer rather than against a hand-written literal.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from campaigns.models import CampaignStatus
from dashboard import services
from messaging.models import MessageStatus

pytestmark = pytest.mark.django_db


class TestAuthorization:
    ENDPOINTS = [
        "dashboard-api:report-overview",
        "dashboard-api:report-activity",
        "dashboard-api:report-campaigns",
        "dashboard-api:report-failures",
        "dashboard-api:report-consent",
        "dashboard-api:active-campaigns",
    ]

    @pytest.mark.parametrize("name", ENDPOINTS)
    def test_anonymous_access_is_refused(self, api_client: APIClient, name: str) -> None:
        assert api_client.get(reverse(name)).status_code in (401, 403)

    @pytest.mark.parametrize("name", ENDPOINTS)
    def test_a_viewer_may_read_every_report(self, viewer, name: str) -> None:
        """Reports are read-only aggregates, so the read-only role can see them."""
        client = APIClient()
        client.force_login(viewer)
        assert client.get(reverse(name)).status_code == 200

    @pytest.mark.parametrize("name", ENDPOINTS)
    def test_a_deactivated_account_loses_access(self, operator, name: str) -> None:
        client = APIClient()
        client.force_login(operator)
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        assert client.get(reverse(name)).status_code in (401, 403)

    @pytest.mark.parametrize("name", ENDPOINTS)
    def test_reports_cannot_be_written(self, auth_api_client: APIClient, name: str, organization) -> None:
        """A report is derived state; the only way to change it is to send."""
        assert auth_api_client.post(reverse(name), {}).status_code == 405


class TestOverviewEndpoint:
    def test_it_reports_the_same_figures_as_the_page(
        self, auth_api_client: APIClient, launched_campaign, make_message, organization
    ) -> None:
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.FAILED)

        body = auth_api_client.get(reverse("dashboard-api:report-overview")).json()
        page = services.overview(organization, services.ReportPeriod.last_days(30))

        assert body["messages"] == page.messages == 2
        assert body["delivery_rate"] == page.delivery_rate
        assert body["failure_rate"] == page.failure_rate

    def test_the_period_is_echoed_back(self, auth_api_client: APIClient) -> None:
        body = auth_api_client.get(reverse("dashboard-api:report-overview"), {"days": 7}).json()
        assert body["period"]["days"] == 7

    def test_an_explicit_range_is_honoured(self, auth_api_client: APIClient) -> None:
        body = auth_api_client.get(
            reverse("dashboard-api:report-overview"), {"start": "2026-08-01", "end": "2026-08-07"}
        ).json()

        assert body["period"]["start"] == "2026-08-01"
        assert body["period"]["end"] == "2026-08-07"

    def test_a_malformed_period_falls_back_instead_of_erroring(
        self, auth_api_client: APIClient
    ) -> None:
        response = auth_api_client.get(
            reverse("dashboard-api:report-overview"), {"days": "lots"}
        )
        assert response.status_code == 200
        assert response.json()["period"]["days"] == services.DEFAULT_PERIOD_DAYS


class TestActivityEndpoint:
    def test_quiet_days_are_returned_as_zeros(
        self, auth_api_client: APIClient, launched_campaign, make_message
    ) -> None:
        """A caller plotting this must not close up the gaps."""
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.SENT)

        body = auth_api_client.get(reverse("dashboard-api:report-activity"), {"days": 5}).json()

        assert len(body["days"]) == 5
        assert [day["total"] for day in body["days"]] == [0, 0, 0, 0, 1]

    def test_the_buckets_sum_to_the_total(
        self, auth_api_client: APIClient, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.QUEUED)
        make_message(campaign, status=MessageStatus.READ)

        day = auth_api_client.get(reverse("dashboard-api:report-activity"), {"days": 1}).json()[
            "days"
        ][0]

        assert day["pending"] + day["sent"] + day["delivered"] + day["read"] + day["failed"] == (
            day["total"]
        )


class TestCampaignsEndpoint:
    def test_only_launched_campaigns_are_returned(
        self, auth_api_client: APIClient, make_campaign, launched_campaign
    ) -> None:
        make_campaign("Draft")
        launched_campaign("Sent")

        body = auth_api_client.get(reverse("dashboard-api:report-campaigns")).json()

        assert [row["name"] for row in body] == ["Sent"]

    def test_a_row_carries_its_rates(
        self, auth_api_client: APIClient, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign("Summer")
        make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.SENT)

        row = auth_api_client.get(reverse("dashboard-api:report-campaigns")).json()[0]

        assert row["total"] == 2
        assert row["delivery_rate"] == 50.0
        assert row["id"] == str(campaign.pk)

    def test_a_campaign_outside_the_period_is_excluded(
        self, auth_api_client: APIClient, launched_campaign
    ) -> None:
        launched_campaign("Old", days_ago=60)
        assert auth_api_client.get(reverse("dashboard-api:report-campaigns"), {"days": 7}).json() == []


class TestActiveCampaignsEndpoint:
    def test_paused_campaigns_are_included(
        self, auth_api_client: APIClient, launched_campaign
    ) -> None:
        launched_campaign("Sending", status=CampaignStatus.PROCESSING)
        launched_campaign("Paused", status=CampaignStatus.PAUSED)
        launched_campaign("Done", status=CampaignStatus.COMPLETED)

        body = auth_api_client.get(reverse("dashboard-api:active-campaigns")).json()

        assert {row["name"] for row in body} == {"Sending", "Paused"}

    def test_it_carries_what_the_live_panel_updates(
        self, auth_api_client: APIClient, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign("Sending", status=CampaignStatus.PROCESSING)
        make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.QUEUED)

        row = auth_api_client.get(reverse("dashboard-api:active-campaigns")).json()[0]

        assert row["progress_percent"] == 50.0
        assert row["pending"] == 1
        assert row["delivered"] == 1

    def test_nothing_sending_is_an_empty_list_not_an_error(
        self, auth_api_client: APIClient
    ) -> None:
        assert auth_api_client.get(reverse("dashboard-api:active-campaigns")).json() == []


class TestFailuresEndpoint:
    def test_errors_are_grouped_and_ordered(
        self, auth_api_client: APIClient, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.FAILED, error_code="470", error_message="Window")
        for _ in range(2):
            make_message(
                campaign, status=MessageStatus.FAILED, error_code="131026", error_message="Bad"
            )

        body = auth_api_client.get(reverse("dashboard-api:report-failures")).json()

        assert [row["error_code"] for row in body] == ["131026", "470"]
        assert body[0]["count"] == 2

    def test_failures_outside_the_period_are_excluded(
        self, auth_api_client: APIClient, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign()
        make_message(
            campaign,
            status=MessageStatus.FAILED,
            error_code="470",
            created_at=timezone.now() - timedelta(days=60),
        )

        assert auth_api_client.get(reverse("dashboard-api:report-failures"), {"days": 7}).json() == []


class TestConsentEndpoint:
    def test_it_reports_current_state(self, auth_api_client: APIClient, make_contact) -> None:
        make_contact("Consenting", opted_in=True)
        make_contact("Not consenting")

        body = auth_api_client.get(reverse("dashboard-api:report-consent")).json()

        assert body["total"] == 2
        assert body["opted_in"] == 1
        assert body["eligible"] == 1
        assert body["opt_in_rate"] == 50.0


class TestSchema:
    def test_the_reporting_endpoints_are_documented(self, auth_api_client: APIClient) -> None:
        """They are part of the published API, not an internal back channel."""
        schema = auth_api_client.get(reverse("api-schema")).content.decode()

        for path in (
            "/api/reports/overview/",
            "/api/reports/activity/",
            "/api/reports/campaigns/",
            "/api/reports/failures/",
            "/api/reports/consent/",
            "/api/monitor/active-campaigns/",
        ):
            assert path in schema, path


class TestNoCredentialLeaks:
    def test_no_endpoint_returns_a_credential(
        self, auth_api_client: APIClient, settings, launched_campaign, make_message
    ) -> None:
        settings.META_ACCESS_TOKEN = "EAAtopsecrettoken1234567890"
        settings.META_APP_SECRET = "topsecretappsecret"
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.SENT)

        for name in TestAuthorization.ENDPOINTS:
            body = auth_api_client.get(reverse(name)).content.decode()
            assert "EAAtopsecrettoken1234567890" not in body, name
            assert "topsecretappsecret" not in body, name
