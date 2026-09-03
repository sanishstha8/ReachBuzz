"""Contact services: normalization, duplicate detection, consent and groups."""

from __future__ import annotations

import pytest

from contacts import services
from contacts.models import Contact, ContactGroup, ContactStatus, GroupMembership, OptInSource
from core.exceptions import ConflictError, ValidationFailed
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


class TestCreateContact:
    def test_normalizes_the_phone_number(self, operator, organization) -> None:
        contact = services.create_contact(
            organization=organization,
            name="Aarav", phone_number="+977 980-000 0000", user=operator
        )
        assert contact.phone_number == "+9779800000000"
        assert contact.country_code == "977"

    def test_defaults_to_not_opted_in(self, operator, organization) -> None:
        contact = services.create_contact(name="Aarav", phone_number="+9779800000000", user=operator, organization=organization)
        assert contact.opted_in is False
        assert contact.opt_in_at is None

    def test_explicit_consent_records_a_source(self, operator, organization) -> None:
        contact = services.create_contact(
            organization=organization,
            name="Aarav",
            phone_number="+9779800000000",
            opted_in=True,
            opt_in_source=OptInSource.WEB_FORM,
            user=operator,
        )
        assert contact.opted_in is True
        assert contact.opt_in_source == OptInSource.WEB_FORM
        assert contact.opt_in_at is not None

    def test_duplicate_number_raises_conflict(self, operator, make_contact, organization) -> None:
        make_contact("Existing", "+9779800000000")

        with pytest.raises(ConflictError) as exc_info:
            services.create_contact(name="Copy", phone_number="+9779800000000", user=operator, organization=organization)

        assert "phone_number" in exc_info.value.details

    def test_duplicate_detection_survives_different_formatting(self, operator, make_contact, organization) -> None:
        """The whole point of normalizing: these are the same person."""
        make_contact("Existing", "+9779800000000")

        with pytest.raises(ConflictError):
            services.create_contact(name="Copy", phone_number="0980-000 0000", user=operator, organization=organization)

    def test_invalid_number_raises_a_field_error(self, operator, organization) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            services.create_contact(name="Bad", phone_number="not a number", user=operator, organization=organization)

        assert "phone_number" in exc_info.value.details

    def test_groups_are_attached(self, operator, group, organization) -> None:
        contact = services.create_contact(
            organization=organization,
            name="Aarav", phone_number="+9779800000000", groups=[group], user=operator
        )
        assert contact.groups.count() == 1

    def test_creation_is_audited(self, operator, organization) -> None:
        contact = services.create_contact(name="Aarav", phone_number="+9779800000000", user=operator, organization=organization)
        entry = AuditLog.objects.get(action=AuditAction.CONTACT_CREATED)
        assert entry.object_id == str(contact.pk)
        assert entry.user == operator


class TestUpdateContact:
    def test_updates_editable_fields(self, operator, make_contact) -> None:
        contact = make_contact("Old Name")
        updated = services.update_contact(contact, name="New Name", user=operator)
        assert updated.name == "New Name"

    def test_changing_to_an_existing_number_raises_conflict(self, operator, make_contact) -> None:
        make_contact("Taken", "+9779800000000")
        contact = make_contact("Mine", "+9779811111111")

        with pytest.raises(ConflictError):
            services.update_contact(contact, phone_number="+9779800000000", user=operator)

    def test_keeping_the_same_number_is_allowed(self, operator, make_contact) -> None:
        contact = make_contact("Mine", "+9779800000000")
        updated = services.update_contact(
            contact, phone_number="+977 980 0000000", name="Renamed", user=operator
        )
        assert updated.phone_number == "+9779800000000"
        assert updated.name == "Renamed"

    def test_consent_cannot_be_changed_through_update(self, operator, make_contact) -> None:
        """A second, unaudited path to consent would defeat the audit trail."""
        contact = make_contact()
        with pytest.raises(ValidationFailed):
            services.update_contact(contact, opted_in=True, user=operator)

    def test_no_audit_entry_when_nothing_changed(self, operator, make_contact) -> None:
        contact = make_contact("Same")
        services.update_contact(contact, name="Same", user=operator)
        assert not AuditLog.objects.filter(action=AuditAction.CONTACT_UPDATED).exists()


class TestDeleteContact:
    def test_removes_the_contact_and_audits_it(self, operator, make_contact) -> None:
        contact = make_contact("Doomed")
        services.delete_contact(contact, user=operator)

        assert Contact.objects.count() == 0
        entry = AuditLog.objects.get(action=AuditAction.CONTACT_DELETED)
        assert entry.metadata["name"] == "Doomed"


class TestSetConsent:
    def test_opt_in_is_recorded_and_audited(self, operator, opted_out_contact) -> None:
        contact = services.set_consent(
            opted_out_contact, opted_in=True, source=OptInSource.MANUAL, user=operator
        )
        contact.refresh_from_db()

        assert contact.opted_in is True
        assert AuditLog.objects.filter(action=AuditAction.CONTACT_OPTED_IN).exists()

    def test_opt_out_is_recorded_and_audited(self, operator, opted_in_contact) -> None:
        contact = services.set_consent(opted_in_contact, opted_in=False, user=operator)
        contact.refresh_from_db()

        assert contact.opted_in is False
        assert contact.opt_out_at is not None
        assert AuditLog.objects.filter(action=AuditAction.CONTACT_OPTED_OUT).exists()

    def test_opted_out_contact_leaves_the_eligible_set(self, operator, opted_in_contact) -> None:
        assert Contact.objects.eligible().count() == 1
        services.set_consent(opted_in_contact, opted_in=False, user=operator)
        assert Contact.objects.eligible().count() == 0


class TestGroupMembership:
    def test_adding_contacts_creates_memberships(self, operator, group, make_contact) -> None:
        contacts = [make_contact(f"C{i}") for i in range(3)]
        services.add_contacts_to_group(group, contacts, user=operator)
        assert group.memberships.count() == 3

    def test_adding_an_existing_member_is_idempotent(self, operator, group, make_contact) -> None:
        contact = make_contact()
        services.add_contacts_to_group(group, [contact], user=operator)
        services.add_contacts_to_group(group, [contact], user=operator)
        assert group.memberships.count() == 1

    def test_removing_contacts_deletes_memberships(self, operator, group, make_contact) -> None:
        contact = make_contact()
        services.add_contacts_to_group(group, [contact], user=operator)

        removed = services.remove_contacts_from_group(group, [contact])

        assert removed == 1
        assert group.memberships.count() == 0

    def test_set_contact_groups_replaces_membership(self, operator, make_contact, organization) -> None:
        contact = make_contact()
        first = ContactGroup.objects.create(name="First", organization=organization)
        second = ContactGroup.objects.create(name="Second", organization=organization)
        third = ContactGroup.objects.create(name="Third", organization=organization)

        services.set_contact_groups(contact, [first, second], user=operator)
        result = services.set_contact_groups(contact, [second, third], user=operator)

        assert result == {"added": 1, "removed": 1}
        assert set(contact.groups.values_list("name", flat=True)) == {"Second", "Third"}

    def test_set_group_members_replaces_the_roster(self, operator, group, make_contact) -> None:
        a, b, c = (make_contact(f"C{i}") for i in range(3))
        services.set_group_members(group, [a, b], user=operator)
        result = services.set_group_members(group, [b, c], user=operator)

        assert result == {"added": 1, "removed": 1}
        assert set(group.contacts.values_list("name", flat=True)) == {"C1", "C2"}


class TestFindDuplicate:
    def test_returns_the_existing_contact(self, make_contact) -> None:
        contact = make_contact("Existing", "+9779800000000")
        assert services.find_duplicate("+9779800000000") == contact

    def test_excludes_the_given_primary_key(self, make_contact) -> None:
        contact = make_contact("Existing", "+9779800000000")
        assert services.find_duplicate("+9779800000000", exclude_pk=contact.pk) is None

    def test_returns_none_when_absent(self, db) -> None:
        assert services.find_duplicate("+9779800000000") is None


class TestStatusEligibility:
    def test_inactive_status_blocks_messaging_even_with_consent(self, operator, make_contact) -> None:
        contact = make_contact(opted_in=True)
        services.update_contact(contact, status=ContactStatus.INACTIVE, user=operator)

        contact.refresh_from_db()
        assert contact.opted_in is True
        assert contact.is_eligible is False


class TestGroupMembershipQueries:
    def test_in_group_filters_correctly(self, group_with_members) -> None:
        assert Contact.objects.in_group(group_with_members).count() == 3
        assert Contact.objects.eligible().in_group(group_with_members).count() == 2

    def test_membership_records_who_added_the_contact(self, operator, group, make_contact) -> None:
        contact = make_contact()
        services.add_contacts_to_group(group, [contact], user=operator)

        membership = GroupMembership.objects.get(group=group, contact=contact)
        assert membership.added_by == operator
