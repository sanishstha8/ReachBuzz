"""
Fixtures specific to the contacts tests.

``make_contact`` lives in the project-root conftest, because the dashboard and
later the campaign tests need an audience too.
"""

from __future__ import annotations

import pytest

from contacts.models import Contact, ContactGroup, GroupMembership


@pytest.fixture
def opted_in_contact(make_contact) -> Contact:
    return make_contact("Aarav Sharma", "+9779800000000", opted_in=True)


@pytest.fixture
def opted_out_contact(make_contact) -> Contact:
    return make_contact("Sita Rai", "+9779811111111", opted_in=False)


@pytest.fixture
def group(db, organization) -> ContactGroup:
    return ContactGroup.objects.create(
        name="Newsletter", description="Monthly updates", organization=organization
    )


@pytest.fixture
def group_with_members(group, make_contact) -> ContactGroup:
    """Three members, two of whom have consented — so the counts differ."""
    for index in range(3):
        contact = make_contact(f"Member {index}", opted_in=index < 2)
        GroupMembership.objects.create(group=group, contact=contact)
    return group
