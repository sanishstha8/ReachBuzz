"""
Isolation at the HTTP layer.

The queryset tests prove the mechanism; these prove it is actually wired to
every door. A customer arrives with a URL and a session, and what comes back
must never be another customer's data — not in a list, not in a detail page,
not in an aggregate, and not in a CSV export.

Aggregates get the same scrutiny as lists on purpose. A count that includes
another tenant's rows leaks just as surely as a page that names them, and it
is far easier to miss in review.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from campaigns.models import Campaign, CampaignStatus
from contacts.models import Contact, ContactGroup
from messaging.models import Message, MessageStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def theirs(other_organization, approved_template):
    """A full set of another customer's records, sharing nothing with ours."""
    contact = Contact.objects.create(
        name="Their Customer",
        phone_number="+9779812340000",
        opted_in=True,
        organization=other_organization,
    )
    group = ContactGroup.objects.create(name="Their Group", organization=other_organization)
    campaign = Campaign.objects.create(
        name="Their Campaign",
        organization=other_organization,
        status=CampaignStatus.COMPLETED,
    )
    message = Message.objects.create(
        campaign=campaign,
        contact=contact,
        to_phone_number=contact.phone_number,
        status=MessageStatus.FAILED,
        error_code="131026",
        error_message="Their failure",
    )
    return {"contact": contact, "group": group, "campaign": campaign, "message": message}


class TestListsDoNotLeak:
    @pytest.mark.parametrize(
        ("url_name", "needle"),
        [
            ("contacts:list", "Their Customer"),
            ("contacts:group-list", "Their Group"),
            ("campaigns:list", "Their Campaign"),
            ("dashboard:home", "Their Campaign"),
        ],
    )
    def test_another_customers_records_are_absent(
        self, auth_client: Client, theirs, url_name, needle
    ) -> None:
        body = auth_client.get(reverse(url_name)).content.decode()
        assert needle not in body


class TestDetailPagesAreNotFound:
    @pytest.mark.parametrize(
        ("url_name", "key"),
        [
            ("contacts:detail", "contact"),
            ("contacts:group-detail", "group"),
            ("campaigns:detail", "campaign"),
        ],
    )
    def test_knowing_the_id_is_not_enough(
        self, auth_client: Client, theirs, url_name, key
    ) -> None:
        """
        404 rather than 403: confirming a record exists but is not yours is
        itself a disclosure.
        """
        response = auth_client.get(reverse(url_name, args=[theirs[key].pk]))
        assert response.status_code == 404


class TestTheApiDoesNotLeak:
    @pytest.mark.parametrize(
        ("path", "needle"),
        [
            ("/api/contacts/", "Their Customer"),
            ("/api/contact-groups/", "Their Group"),
            ("/api/campaigns/", "Their Campaign"),
            ("/api/messages/", "+9779812340000"),
        ],
    )
    def test_lists_exclude_other_tenants(self, auth_api_client, theirs, path, needle) -> None:
        body = auth_api_client.get(path).content.decode()
        assert needle not in body

    @pytest.mark.parametrize(
        ("path", "key"),
        [("/api/contacts/", "contact"), ("/api/campaigns/", "campaign")],
    )
    def test_retrieving_by_id_is_not_found(self, auth_api_client, theirs, path, key) -> None:
        assert auth_api_client.get(f"{path}{theirs[key].pk}/").status_code == 404

    def test_a_write_cannot_reach_across(self, auth_api_client, theirs) -> None:
        """Nor may an update target somebody else's row."""
        response = auth_api_client.patch(
            f"/api/contacts/{theirs['contact'].pk}/", {"name": "Renamed"}, format="json"
        )
        assert response.status_code == 404
        theirs["contact"].refresh_from_db()
        assert theirs["contact"].name == "Their Customer"


class TestAggregatesDoNotLeak:
    """A count that includes another tenant is a leak wearing a number."""

    def test_contact_statistics_count_only_our_own(self, auth_api_client, theirs) -> None:
        body = auth_api_client.get("/api/contacts/stats/").json()
        assert body["total"] == 0

    def test_message_statistics_count_only_our_own(self, auth_api_client, theirs) -> None:
        body = auth_api_client.get("/api/messages/stats/").json()
        assert body["total"] == 0

    def test_the_reporting_api_counts_only_our_own(self, auth_api_client, theirs) -> None:
        body = auth_api_client.get(reverse("dashboard-api:report-overview")).json()
        assert body["messages"] == 0
        assert body["failed"] == 0

    def test_the_consent_summary_counts_only_our_own(self, auth_api_client, theirs) -> None:
        body = auth_api_client.get(reverse("dashboard-api:report-consent")).json()
        assert body["total"] == 0

    def test_the_failure_breakdown_excludes_other_tenants(
        self, auth_api_client, theirs
    ) -> None:
        body = auth_api_client.get(reverse("dashboard-api:report-failures")).content.decode()
        assert "Their failure" not in body

    def test_the_reports_page_shows_none_of_it(self, auth_client: Client, theirs) -> None:
        body = auth_client.get(reverse("dashboard:reports")).content.decode()
        assert "Their Campaign" not in body
        assert "Their failure" not in body


class TestExportsDoNotLeak:
    """The worst version of the bug: another customer's data on your disk."""

    @pytest.mark.parametrize(
        ("report", "needle"),
        [
            ("campaigns", "Their Campaign"),
            ("messages", "+9779812340000"),
            ("consent", "Their Customer"),
            ("failures", "Their failure"),
        ],
    )
    def test_a_csv_export_contains_no_other_tenant(
        self, auth_client: Client, theirs, report, needle
    ) -> None:
        response = auth_client.get(
            reverse("dashboard:report-download", kwargs={"report": report})
        )
        body = b"".join(response.streaming_content).decode()
        assert needle not in body
