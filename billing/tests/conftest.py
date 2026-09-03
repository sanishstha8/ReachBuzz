"""Fixtures for the billing tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def sendable(organization, make_campaign, approved_template, make_contact):
    """
    A campaign that would launch if no plan stood in the way.

    Built here rather than borrowed from whatsapp/tests, whose conftest is not
    visible from this directory. Everything about it is valid, so a test that
    sees it refused knows the refusal came from the quota.
    """
    from campaigns.models import CampaignMessageType
    from campaigns.services import set_audience, set_message
    from contacts.models import ContactGroup, GroupMembership

    group = ContactGroup.objects.create(name="Everyone", organization=organization)
    for name in ("Ann", "Bob"):
        GroupMembership.objects.create(
            group=group, contact=make_contact(name, opted_in=True)
        )

    campaign = make_campaign("Launchable")
    set_audience(campaign, [group])
    set_message(
        campaign,
        message_type=CampaignMessageType.TEMPLATE,
        template=approved_template,
        variable_mapping={
            "name": {"source": "contact_field", "value": "name"},
            "order_id": {"source": "literal", "value": "A-1"},
        },
    )
    return campaign
