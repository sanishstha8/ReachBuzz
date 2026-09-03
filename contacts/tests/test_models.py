"""Contact, group and membership model behaviour."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from contacts.models import (
    Contact,
    ContactGroup,
    ContactStatus,
    GroupMembership,
    OptInSource,
    OptOutSource,
)

pytestmark = pytest.mark.django_db


class TestContact:
    def test_phone_number_is_unique(self, make_contact, organization) -> None:
        make_contact("First", "+9779800000000")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Contact.objects.create(name="Second", phone_number="+9779800000000", organization=organization)

    def test_non_e164_number_violates_the_check_constraint(self, db, organization) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Contact.objects.create(name="Bad", phone_number="9779800000000", organization=organization)

    def test_new_contacts_are_not_opted_in_by_default(self, db, organization) -> None:
        contact = Contact.objects.create(name="Nobody", phone_number="+9779800000001", organization=organization)
        assert contact.opted_in is False
        assert contact.opt_in_at is None

    def test_str_includes_name_and_number(self, opted_in_contact) -> None:
        assert str(opted_in_contact) == "Aarav Sharma <+9779800000000>"


class TestConsentState:
    def test_opt_in_records_source_and_timestamp(self, make_contact) -> None:
        contact = make_contact()
        contact.opt_in(OptInSource.WEB_FORM)

        assert contact.opted_in is True
        assert contact.opt_in_source == OptInSource.WEB_FORM
        assert contact.opt_in_at is not None

    def test_opt_in_clears_a_previous_opt_out(self, make_contact) -> None:
        contact = make_contact()
        contact.opt_out(OptOutSource.INBOUND_STOP)
        contact.opt_in(OptInSource.MANUAL)

        assert contact.opt_out_at is None
        assert contact.opt_out_source == ""

    def test_opt_out_records_source_and_timestamp(self, opted_in_contact) -> None:
        opted_in_contact.opt_out(OptOutSource.INBOUND_STOP)

        assert opted_in_contact.opted_in is False
        assert opted_in_contact.opt_out_source == OptOutSource.INBOUND_STOP
        assert opted_in_contact.opt_out_at is not None


class TestEligibility:
    def test_only_opted_in_and_active_contacts_are_eligible(self, make_contact) -> None:
        eligible = make_contact("Eligible", opted_in=True)
        make_contact("Not opted in", opted_in=False)
        make_contact("Inactive", opted_in=True, status=ContactStatus.INACTIVE)
        make_contact("Blocked", opted_in=True, status=ContactStatus.BLOCKED)
        make_contact("Invalid number", opted_in=True, status=ContactStatus.INVALID)

        assert list(Contact.objects.eligible()) == [eligible]

    def test_is_eligible_property_matches_the_queryset(self, make_contact) -> None:
        contact = make_contact(opted_in=True, status=ContactStatus.INACTIVE)
        assert contact.is_eligible is False
        assert not Contact.objects.eligible().filter(pk=contact.pk).exists()

    def test_search_matches_name_number_and_email(self, make_contact) -> None:
        make_contact("Aarav Sharma", "+9779800000000", email="aarav@example.com")
        make_contact("Sita Rai", "+9779811111111")

        assert Contact.objects.search("aarav").count() == 1
        assert Contact.objects.search("9811111111").count() == 1
        assert Contact.objects.search("example.com").count() == 1
        assert Contact.objects.search("").count() == 2


class TestGroups:
    def test_a_contact_can_belong_to_several_groups(self, make_contact, organization) -> None:
        contact = make_contact()
        first = ContactGroup.objects.create(name="First", organization=organization)
        second = ContactGroup.objects.create(name="Second", organization=organization)

        GroupMembership.objects.create(group=first, contact=contact)
        GroupMembership.objects.create(group=second, contact=contact)

        assert contact.groups.count() == 2

    def test_duplicate_membership_is_rejected(self, make_contact, group) -> None:
        contact = make_contact()
        GroupMembership.objects.create(group=group, contact=contact)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                GroupMembership.objects.create(group=group, contact=contact)

    def test_group_name_is_unique(self, group, organization) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ContactGroup.objects.create(name=group.name, organization=organization)

    def test_member_and_eligible_counts_differ(self, group_with_members) -> None:
        assert group_with_members.count_members() == 3
        assert group_with_members.count_eligible() == 2

    def test_with_counts_annotates_both_totals_in_one_query(
        self, group_with_members, django_assert_num_queries
    ) -> None:
        """The group table must not issue two extra queries per row."""
        with django_assert_num_queries(1):
            group = ContactGroup.objects.with_counts().get(pk=group_with_members.pk)
            assert group.member_count == 3
            assert group.eligible_count == 2

    def test_deleting_a_group_keeps_the_contacts(self, group_with_members) -> None:
        group_with_members.delete()
        assert Contact.objects.count() == 3

    def test_deleting_a_contact_removes_its_memberships(self, group_with_members) -> None:
        contact = Contact.objects.first()
        contact.delete()
        assert GroupMembership.objects.filter(contact_id=contact.pk).count() == 0
