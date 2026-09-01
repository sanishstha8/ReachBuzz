"""Fixtures for the sending tests."""

from __future__ import annotations

import pytest

from campaigns.models import CampaignMessageType
from campaigns.services import set_audience, set_message
from contacts.models import ContactGroup, GroupMembership


@pytest.fixture
def group(db) -> ContactGroup:
    return ContactGroup.objects.create(name="Newsletter")


@pytest.fixture
def celery_dispatcher():
    """
    Register the real Celery dispatcher.

    The project-wide autouse fixture clears any dispatcher before each test, so
    a test that wants the genuine send path has to ask for it explicitly.
    """
    from campaigns import dispatch
    from whatsapp import tasks

    dispatch.register_dispatcher(tasks.queue_campaign)
    try:
        yield tasks.queue_campaign
    finally:
        dispatch.clear_dispatcher()


@pytest.fixture
def ready_campaign(make_campaign, approved_template, group, make_contact):
    """A validated campaign with three consenting recipients."""
    for index in range(3):
        GroupMembership.objects.create(
            group=group, contact=make_contact(f"Member {index}", opted_in=True)
        )

    campaign = make_campaign("Summer Sale")
    set_audience(campaign, [group])
    set_message(
        campaign,
        message_type=CampaignMessageType.TEMPLATE,
        template=approved_template,
        variable_mapping={
            "name": {"source": "contact_field", "value": "name"},
            "order_id": {"source": "literal", "value": "A-100"},
        },
    )
    return campaign


@pytest.fixture
def launched_campaign(ready_campaign, celery_dispatcher, django_capture_on_commit_callbacks):
    """A campaign that has been launched, with its messages materialized."""
    from campaigns.services import launch_campaign

    with django_capture_on_commit_callbacks(execute=True):
        launch_campaign(ready_campaign)
    ready_campaign.refresh_from_db()
    return ready_campaign
