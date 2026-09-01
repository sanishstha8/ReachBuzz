"""Fixtures for the campaign tests (group helpers shared with contacts)."""

from __future__ import annotations

import pytest

from contacts.models import ContactGroup


@pytest.fixture
def group(db) -> ContactGroup:
    return ContactGroup.objects.create(name="Newsletter", description="Monthly updates")
