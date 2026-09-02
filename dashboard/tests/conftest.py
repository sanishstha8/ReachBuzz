"""Fixtures for the reporting tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from campaigns.models import CampaignStatus
from messaging.models import Message, MessageStatus


@pytest.fixture
def make_message(db, make_contact):
    """
    Create a message with a chosen status and creation date.

    ``created_at`` is ``auto_now_add``, so a test that needs a message dated
    last Tuesday has to write the column afterwards — which is what this hides.
    Every report groups on that column, so almost every test here needs it.
    """
    counter = {"n": 0}

    def _make_message(
        campaign,
        *,
        status: str = MessageStatus.PENDING,
        created_at: datetime | None = None,
        contact=None,
        error_code: str = "",
        error_message: str = "",
    ) -> Message:
        counter["n"] += 1
        contact = contact or make_contact(f"Recipient {counter['n']}", opted_in=True)

        message = Message.objects.create(
            campaign=campaign,
            contact=contact,
            to_phone_number=contact.phone_number,
            status=status,
            error_code=error_code,
            error_message=error_message,
            failed_at=timezone.now() if status == MessageStatus.FAILED else None,
        )
        if created_at is not None:
            Message.objects.filter(pk=message.pk).update(created_at=created_at)
            message.refresh_from_db()
        return message

    return _make_message


@pytest.fixture
def launched_campaign(make_campaign):
    """A campaign that has actually been sent, which is what reports cover."""

    def _launched(name: str = "Launched", *, days_ago: int = 0, status: str = CampaignStatus.COMPLETED):
        campaign = make_campaign(name, status=status)
        campaign.started_at = timezone.now() - timedelta(days=days_ago)
        campaign.save(update_fields=["started_at"])
        return campaign

    return _launched
