"""
Consent, one channel at a time.

This is the safety-critical half of adding SMS, and the assertions are almost
all negative. The failure being prevented is a silent one: adding a second
channel on top of a single consent flag would have opted every existing contact
in to it, and nothing would have looked wrong. Everybody who had agreed to a
WhatsApp delivery notice would have started receiving text messages.
"""

from __future__ import annotations

import pytest

from contacts.consent import ContactChannelConsent
from contacts.models import Contact, ContactStatus, OptInSource
from contacts.services import set_consent
from core.channels import Channel
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


class TestConsentIsNotInferredAcrossChannels:
    def test_whatsapp_consent_does_not_grant_sms(self, make_contact) -> None:
        """
        The whole reason this stage was not a two-line change. A contact who
        accepted a delivery notification has not agreed to a text message.
        """
        contact = make_contact("Agreed to WhatsApp", opted_in=True)

        assert contact in Contact.objects.eligible(Channel.WHATSAPP)
        assert contact not in Contact.objects.eligible(Channel.SMS)

    def test_sms_consent_does_not_grant_whatsapp(self, make_contact) -> None:
        """It fails in the other direction too, which is the same rule."""
        contact = make_contact("Agreed to SMS", opted_in=False)
        set_consent(contact, opted_in=True, channel=Channel.SMS, source=OptInSource.MANUAL)

        assert contact in Contact.objects.eligible(Channel.SMS)
        assert contact not in Contact.objects.eligible(Channel.WHATSAPP)

    def test_no_record_means_no_consent(self, make_contact) -> None:
        """
        Absence is a "no", never a "not asked yet, so probably fine". A channel
        added tomorrow starts with nobody on it.
        """
        contact = make_contact("Never asked", opted_in=True)

        assert not ContactChannelConsent.objects.filter(contact=contact).exists()
        assert contact not in Contact.objects.eligible(Channel.SMS)

    def test_both_can_be_held_at_once(self, make_contact) -> None:
        contact = make_contact("Agreed to both", opted_in=True)
        set_consent(contact, opted_in=True, channel=Channel.SMS)

        assert contact in Contact.objects.eligible(Channel.WHATSAPP)
        assert contact in Contact.objects.eligible(Channel.SMS)

    def test_withdrawing_one_leaves_the_other(self, make_contact) -> None:
        """A person unsubscribing from texts has not left WhatsApp."""
        contact = make_contact("Agreed to both", opted_in=True)
        set_consent(contact, opted_in=True, channel=Channel.SMS)

        set_consent(contact, opted_in=False, channel=Channel.SMS)

        assert contact in Contact.objects.eligible(Channel.WHATSAPP)
        assert contact not in Contact.objects.eligible(Channel.SMS)

    def test_the_default_channel_is_whatsapp(self, make_contact) -> None:
        """Every caller written before channels existed keeps its meaning."""
        contact = make_contact("Legacy", opted_in=True)

        assert contact in Contact.objects.eligible()


class TestTheOtherEligibilityRulesStillApply:
    def test_an_inactive_contact_is_not_eligible_on_any_channel(
        self, make_contact
    ) -> None:
        """Consent is necessary, not sufficient. Status still counts."""
        contact = make_contact("Blocked", opted_in=True, status=ContactStatus.BLOCKED)
        set_consent(contact, opted_in=True, channel=Channel.SMS)

        assert contact not in Contact.objects.eligible(Channel.WHATSAPP)
        assert contact not in Contact.objects.eligible(Channel.SMS)

    def test_eligibility_stays_scoped_to_the_organization(
        self, organization, other_organization, make_contact
    ) -> None:
        """Channel filtering must not have quietly widened the tenant filter."""
        theirs = make_contact("Theirs", opted_in=True, organization=other_organization)
        set_consent(theirs, opted_in=True, channel=Channel.SMS)

        mine = Contact.objects.for_organization(organization).eligible(Channel.SMS)

        assert theirs not in mine
        assert mine.count() == 0

    def test_an_unresolved_organization_is_still_nothing(self, make_contact) -> None:
        contact = make_contact("Somebody", opted_in=True)
        set_consent(contact, opted_in=True, channel=Channel.SMS)

        assert Contact.objects.for_organization(None).eligible(Channel.SMS).count() == 0


class TestItIsRecordedProperly:
    def test_granting_writes_a_row_with_a_source_and_a_time(self, make_contact) -> None:
        contact = make_contact("Agreed")

        set_consent(contact, opted_in=True, channel=Channel.SMS, source=OptInSource.CSV_IMPORT)

        record = ContactChannelConsent.objects.get(contact=contact, channel=Channel.SMS)
        assert record.opted_in is True
        assert record.source == OptInSource.CSV_IMPORT
        assert record.opted_in_at is not None

    def test_it_is_audited_with_the_channel(self, make_contact) -> None:
        """
        Same two audit actions as WhatsApp, not new ones: somebody reading a
        contact's consent history wants one list in date order.
        """
        contact = make_contact("Agreed")

        set_consent(contact, opted_in=True, channel=Channel.SMS)

        entry = AuditLog.objects.filter(action=AuditAction.CONTACT_OPTED_IN).latest("created_at")
        assert entry.metadata["channel"] == Channel.SMS
        assert "SMS" in entry.description

    def test_changing_a_decision_updates_the_row_rather_than_adding_one(
        self, make_contact
    ) -> None:
        """Two rows disagreeing about whether somebody said yes is unreachable."""
        contact = make_contact("Changed their mind")

        set_consent(contact, opted_in=True, channel=Channel.SMS)
        set_consent(contact, opted_in=False, channel=Channel.SMS)
        set_consent(contact, opted_in=True, channel=Channel.SMS)

        assert ContactChannelConsent.objects.filter(contact=contact).count() == 1

    def test_the_database_refuses_a_duplicate(self, make_contact) -> None:
        from django.db import IntegrityError, transaction

        contact = make_contact("Once")
        ContactChannelConsent.objects.create(contact=contact, channel=Channel.SMS)

        with pytest.raises(IntegrityError), transaction.atomic():
            ContactChannelConsent.objects.create(contact=contact, channel=Channel.SMS)

    def test_withdrawing_stamps_the_time(self, make_contact) -> None:
        contact = make_contact("Left")
        set_consent(contact, opted_in=True, channel=Channel.SMS)

        set_consent(contact, opted_in=False, channel=Channel.SMS)

        record = ContactChannelConsent.objects.get(contact=contact, channel=Channel.SMS)
        assert record.opted_out_at is not None
        assert record.opted_in is False

    def test_whatsapp_still_writes_the_original_column(self, make_contact) -> None:
        """
        The existing path is untouched. Its column carries every audited
        decision this system has ever made and moving it is a later job.
        """
        contact = make_contact("Legacy")

        set_consent(contact, opted_in=True)

        contact.refresh_from_db()
        assert contact.opted_in is True
        assert not ContactChannelConsent.objects.filter(contact=contact).exists()
