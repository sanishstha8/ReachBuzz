"""
The retrofit migration, tested against data.

Applying it to the development database only proved the empty path — there was
nothing to assign. The path that matters is the other one: a system already in
use, whose contacts and campaigns must come out the far side owned by somebody
rather than orphaned.

The migration's own functions are called directly with the live app registry.
That is not quite what Django hands them (a historical registry, frozen at that
point in the graph), but the logic under test is the assignment, and this
exercises it against real rows without a migration-test harness.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as live_apps

from campaigns.models import Campaign
from contacts.models import Contact, ContactGroup
from messaging.models import Message
from organizations.models import Organization, OrganizationMember
from whatsapp.models import MessageTemplate

pytestmark = pytest.mark.django_db

# The module name starts with a digit, so it cannot be imported by name.
_module = importlib.import_module(
    "organizations.migrations.0002_backfill_default_organization"
)


@pytest.fixture
def unowned(make_user):
    """Rows as they looked before the retrofit: real data, no organization."""
    Organization.objects.all().delete()

    owner = make_user("owner@example.com")
    owner.is_superuser = True
    owner.save(update_fields=["is_superuser"])
    colleague = make_user("colleague@example.com")

    contact = Contact.objects.create(name="Existing", phone_number="+9779800000077")
    group = ContactGroup.objects.create(name="Existing group")
    campaign = Campaign.objects.create(name="Existing campaign")
    template = MessageTemplate.objects.create(name="existing", body_text="hi")
    message = Message.objects.create(
        campaign=campaign, contact=contact, to_phone_number=contact.phone_number
    )
    return {
        "owner": owner,
        "colleague": colleague,
        "rows": [contact, group, campaign, template, message],
    }


def run_backfill():
    _module.backfill(live_apps, None)


class TestBackfill:
    def test_it_creates_one_organization(self, unowned) -> None:
        run_backfill()

        assert Organization.objects.count() == 1
        assert Organization.objects.get().name == "Default Organization"

    def test_the_superuser_becomes_the_owner(self, unowned) -> None:
        """Whoever has been running the system is who its data belongs to."""
        run_backfill()

        assert Organization.objects.get().owner == unowned["owner"]

    def test_every_existing_row_gets_an_owner(self, unowned) -> None:
        run_backfill()

        organization = Organization.objects.get()
        for row in unowned["rows"]:
            row.refresh_from_db()
            assert row.organization_id == organization.pk, type(row).__name__

    def test_every_user_gets_a_seat(self, unowned) -> None:
        run_backfill()

        assert OrganizationMember.objects.count() == 2
        roles = dict(OrganizationMember.objects.values_list("user__email", "role"))
        assert roles["owner@example.com"] == "owner"
        assert roles["colleague@example.com"] in {"admin", "member"}

    def test_running_it_twice_changes_nothing(self, unowned) -> None:
        """Migrations get re-run against restored snapshots; it must be safe."""
        run_backfill()
        run_backfill()

        assert Organization.objects.count() == 1
        assert OrganizationMember.objects.count() == 2

    def test_it_does_nothing_on_a_fresh_install(self) -> None:
        """
        No users means no owner and, necessarily, no data needing one. It must
        not invent an organization nobody belongs to.
        """
        from django.contrib.auth import get_user_model

        Organization.objects.all().delete()
        get_user_model().objects.all().delete()

        run_backfill()

        assert Organization.objects.count() == 0

    def test_it_leaves_rows_that_already_have_an_owner_alone(
        self, unowned, make_user
    ) -> None:
        """A partially-migrated database must not have its assignments rewritten."""
        already = Organization.objects.create(name="Already Mine", owner=unowned["owner"])
        settled = Contact.objects.create(
            name="Settled", phone_number="+9779800000088", organization=already
        )

        run_backfill()

        settled.refresh_from_db()
        assert settled.organization_id == already.pk


class TestReverse:
    def test_reversing_detaches_rather_than_deletes(self, unowned) -> None:
        """
        Reversing a migration must not destroy the data whose ownership it was
        reversing — deleting the organization would cascade to all of it.
        """
        run_backfill()
        before = Contact.objects.count()

        _module.unbackfill(live_apps, None)

        assert Contact.objects.count() == before
        assert Contact.objects.filter(organization__isnull=True).count() == before
        assert Organization.objects.exists()
