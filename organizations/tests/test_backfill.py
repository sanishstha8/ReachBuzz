"""
The retrofit migration, as far as it can still be tested.

Most of what this file used to cover is no longer expressible. The backfill
runs between the migration that adds ``organization`` as nullable and the one
that makes it required, and its whole job is to turn rows with no tenant into
rows with one. Now that the column is non-null, a row without a tenant cannot
be created at all — the database rejects it — so the before state has no
representation against the current schema.

Testing it properly needs a harness that builds the historical model state at
a chosen point in the graph (``django-test-migrations`` is the usual one). That
is a dependency decision, not something to slip in here.

What remains testable is the logic that does not need unowned rows: the guard
for a fresh install, and the mapping from the platform roles this project
already had onto seats in the organization. The assignment itself was verified
by applying the migration to the real development database, which reported
``organizations.0002_backfill_default_organization... OK`` and produced the
Default Organization with its owner.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as live_apps
from django.contrib.auth import get_user_model

from organizations.models import Organization, OrganizationMember

pytestmark = pytest.mark.django_db

_module = importlib.import_module(
    "organizations.migrations.0002_backfill_default_organization"
)


def run_backfill():
    _module.backfill(live_apps, None)


class TestFreshInstall:
    def test_it_creates_nothing_when_there_are_no_users(self) -> None:
        """
        No users means no owner and, necessarily, no data needing one. It must
        not invent an organization that nobody belongs to.
        """
        Organization.objects.all().delete()
        get_user_model().objects.all().delete()

        run_backfill()

        assert Organization.objects.count() == 0


class TestSeats:
    @pytest.fixture
    def users(self, make_user):
        Organization.objects.all().delete()
        owner = make_user("owner@example.com")
        owner.is_superuser = True
        owner.save(update_fields=["is_superuser"])
        return {"owner": owner, "colleague": make_user("colleague@example.com")}

    def test_the_superuser_becomes_the_owner(self, users) -> None:
        """Whoever has been running the system is who its data belongs to."""
        run_backfill()

        assert Organization.objects.get().owner == users["owner"]

    def test_every_user_gets_a_seat(self, users) -> None:
        run_backfill()

        roles = dict(OrganizationMember.objects.values_list("user__email", "role"))
        assert roles["owner@example.com"] == "owner"
        assert roles["colleague@example.com"] in {"admin", "member"}

    def test_running_it_twice_changes_nothing(self, users) -> None:
        """Migrations get re-run against restored snapshots; it must be safe."""
        run_backfill()
        run_backfill()

        assert Organization.objects.count() == 1
        assert OrganizationMember.objects.count() == 2

    def test_the_role_map_covers_every_platform_role(self) -> None:
        """A role the map forgets would silently become a plain member."""
        from accounts.models import UserRole

        assert set(_module.ROLE_MAP) == set(UserRole.values)
