"""
No page may issue more queries as the database grows.

This asserts a *property*, not a number. Pinning exact counts makes a test that
fails every time someone legitimately adds a query, so it gets bumped without
being read and stops meaning anything. What actually matters is the shape: a
page that costs the same at 3 rows and at 30 is doing its joins properly, and
one that costs more is doing a query per row and will fall over at a thousand.

An N+1 is invisible in development — 30 extra queries against a local database
is a few milliseconds — and obvious in production. This is the cheapest place
to catch one.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from campaigns.models import CampaignStatus
from contacts.models import ContactGroup, GroupMembership
from messaging.models import Message, MessageStatus

pytestmark = pytest.mark.django_db

SMALL = 3
LARGER = 30


def build_data(size, make_contact, make_campaign, template):
    """A group, a completed campaign, and ``size`` recipients with messages."""
    group = ContactGroup.objects.create(name=f"Group of {size}")
    campaign = make_campaign(f"Campaign of {size}", status=CampaignStatus.COMPLETED, template=template)

    contacts = []
    for index in range(size):
        contact = make_contact(f"Contact {index}", opted_in=True)
        GroupMembership.objects.create(group=group, contact=contact)
        Message.objects.create(
            campaign=campaign,
            contact=contact,
            to_phone_number=contact.phone_number,
            status=MessageStatus.DELIVERED if index % 2 else MessageStatus.FAILED,
            error_code="131026" if index % 2 == 0 else "",
            error_message="Message undeliverable" if index % 2 == 0 else "",
            provider_message_id=f"wamid.{size}.{index}",
        )
        contacts.append(contact)

    return {"group": group, "campaign": campaign, "contacts": contacts}


def pages(data) -> dict[str, str]:
    return {
        "contact list": reverse("contacts:list"),
        "contact detail": reverse("contacts:detail", args=[data["contacts"][0].pk]),
        "group list": reverse("contacts:group-list"),
        "group detail": reverse("contacts:group-detail", args=[data["group"].pk]),
        "import list": reverse("contacts:import-list"),
        "campaign list": reverse("campaigns:list"),
        "campaign detail": reverse("campaigns:detail", args=[data["campaign"].pk]),
        "campaign recipients": reverse("campaigns:messages", args=[data["campaign"].pk]),
        "template list": reverse("whatsapp:template-list"),
        "dashboard": reverse("dashboard:home"),
        "reports": reverse("dashboard:reports"),
    }


def count_queries(client, url: str) -> int:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
    return len(captured)


class TestPagesDoNotScaleWithData:
    def test_every_page_costs_the_same_at_any_size(
        self, auth_client, make_contact, make_campaign, approved_template
    ) -> None:
        small = build_data(SMALL, make_contact, make_campaign, approved_template)
        cheap = {name: count_queries(auth_client, url) for name, url in pages(small).items()}

        larger = build_data(LARGER, make_contact, make_campaign, approved_template)
        dear = {name: count_queries(auth_client, url) for name, url in pages(larger).items()}

        grew = {
            name: (cheap[name], dear[name]) for name in cheap if dear[name] > cheap[name]
        }
        assert not grew, f"query count grew with the data (page: small -> large): {grew}"


class TestApiDoesNotScaleWithData:
    def test_every_list_endpoint_costs_the_same_at_any_size(
        self, auth_api_client, make_contact, make_campaign, approved_template
    ) -> None:
        """
        The API pages at 25 by default, so an N+1 here is a query per row of a
        page — the same defect, reached through a different door.
        """
        endpoints = {
            "contacts": "/api/contacts/",
            "groups": "/api/contact-groups/",
            "campaigns": "/api/campaigns/",
            "messages": "/api/messages/",
            "templates": "/api/templates/",
            "report campaigns": "/api/reports/campaigns/",
            "report activity": "/api/reports/activity/",
            "active campaigns": "/api/monitor/active-campaigns/",
        }

        build_data(SMALL, make_contact, make_campaign, approved_template)
        cheap = {name: count_queries(auth_api_client, url) for name, url in endpoints.items()}

        build_data(LARGER, make_contact, make_campaign, approved_template)
        dear = {name: count_queries(auth_api_client, url) for name, url in endpoints.items()}

        grew = {name: (cheap[name], dear[name]) for name in cheap if dear[name] > cheap[name]}
        assert not grew, f"query count grew with the data (endpoint: small -> large): {grew}"


class TestExportsStreamRatherThanBuffer:
    def test_a_large_export_does_not_scale_its_query_count(
        self, auth_client, make_contact, make_campaign, approved_template
    ) -> None:
        """
        Exports read with .iterator(), so the cost is a fixed number of chunked
        queries rather than one per row.
        """
        from dashboard.reports import REPORTS

        build_data(SMALL, make_contact, make_campaign, approved_template)
        cheap = {}
        for slug in REPORTS:
            with CaptureQueriesContext(connection) as captured:
                response = auth_client.get(
                    reverse("dashboard:report-download", kwargs={"report": slug})
                )
                b"".join(response.streaming_content)
            cheap[slug] = len(captured)

        build_data(LARGER, make_contact, make_campaign, approved_template)
        for slug in REPORTS:
            with CaptureQueriesContext(connection) as captured:
                response = auth_client.get(
                    reverse("dashboard:report-download", kwargs={"report": slug})
                )
                b"".join(response.streaming_content)
            assert len(captured) <= cheap[slug], slug
