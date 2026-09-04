"""
Who gets into the backoffice, and what it records.

This is the one app in the project that reads across the tenant boundary, so its
tests are almost entirely about the gate rather than the pages. Three properties
carry the weight:

1. **Only staff get in.** Not an organization owner, not a customer's own
   administrator, not a platform "administrator" by ``User.role`` — those are
   different things, and Stage 1 separated them for exactly this reason.
2. **A signed-in customer gets a 404, not a 403.** A 403 confirms something
   exists at that address.
3. **Opening a customer's page is recorded**, before the page renders, naming
   who looked and what they looked at.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from accounts.models import UserRole
from core.models import AuditAction, AuditLog
from organizations.models import OrganizationMember, OrganizationRole

pytestmark = pytest.mark.django_db

AGGREGATE_PAGES = ["backoffice:overview", "backoffice:organizations", "backoffice:health"]


@pytest.fixture
def staff(make_user):
    """Somebody who works for the platform."""
    return make_user("staff@example.com", is_staff=True)


@pytest.fixture
def staff_client(client, staff):
    client.force_login(staff)
    return client


class TestTheGate:
    @pytest.mark.parametrize("name", AGGREGATE_PAGES)
    def test_an_anonymous_visitor_is_sent_to_sign_in(self, client, name: str) -> None:
        """A redirect leaks nothing the login page does not."""
        response = client.get(reverse(name))

        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    @pytest.mark.parametrize("name", AGGREGATE_PAGES)
    def test_a_signed_in_customer_gets_a_404(self, auth_client, name: str) -> None:
        """Not a 403: that would confirm the backoffice exists at this address."""
        assert auth_client.get(reverse(name)).status_code == 404

    def test_an_organization_owner_is_still_refused(
        self, auth_client, organization, operator
    ) -> None:
        """
        Owning a business is not working for the platform. Conflating the two is
        precisely how a customer's administrator ends up reading somebody else's
        data, which is what Stage 1 split these roles to prevent.
        """
        membership = OrganizationMember.objects.get(organization=organization, user=operator)
        assert membership.role == OrganizationRole.OWNER

        assert auth_client.get(reverse("backoffice:overview")).status_code == 404

    def test_the_platform_administrator_role_is_not_enough(
        self, client, make_user
    ) -> None:
        """
        ``User.role`` says what somebody may do inside the product. ``is_staff``
        says they work for whoever runs it. Only the second opens this door.
        """
        administrator = make_user("admin@example.com", role=UserRole.ADMINISTRATOR)
        client.force_login(administrator)

        assert client.get(reverse("backoffice:overview")).status_code == 404

    @pytest.mark.parametrize("name", AGGREGATE_PAGES)
    def test_staff_get_in(self, staff_client, name: str) -> None:
        assert staff_client.get(reverse(name)).status_code == 200

    def test_an_inactive_staff_account_cannot_sign_in_at_all(
        self, client, make_user
    ) -> None:
        locked = make_user("gone@example.com", is_staff=True, is_active=False)
        client.force_login(locked)

        response = client.get(reverse("backoffice:overview"))

        assert response.status_code in {302, 404}

    def test_a_refusal_is_logged(self, auth_client, caplog) -> None:
        """Somebody probing this URL is worth knowing about."""
        with caplog.at_level("WARNING", logger="backoffice.access"):
            auth_client.get(reverse("backoffice:overview"))

        assert "tried to reach the backoffice" in caplog.text


class TestLookingIsRecorded:
    def test_opening_a_customer_writes_an_audit_entry(
        self, staff_client, organization, staff
    ) -> None:
        staff_client.get(reverse("backoffice:organization-detail", args=[organization.pk]))

        entry = AuditLog.objects.get(action=AuditAction.BACKOFFICE_VIEWED)
        assert entry.user == staff
        assert entry.metadata["organization"] == str(organization.pk)

    def test_the_entry_names_the_organization_looked_at(
        self, staff_client, organization
    ) -> None:
        staff_client.get(reverse("backoffice:organization-detail", args=[organization.pk]))

        entry = AuditLog.objects.get(action=AuditAction.BACKOFFICE_VIEWED)
        assert organization.name in entry.description

    def test_a_refused_visitor_writes_nothing(self, auth_client, organization) -> None:
        """
        The staff check runs before the target is resolved. Otherwise a stranger's
        request would write an audit entry naming a customer they have no right
        to see — and would run the lookup to do it.
        """
        auth_client.get(reverse("backoffice:organization-detail", args=[organization.pk]))

        assert not AuditLog.objects.filter(action=AuditAction.BACKOFFICE_VIEWED).exists()

    def test_the_aggregate_pages_are_not_audited(self, staff_client) -> None:
        """
        A count of organizations is not a look at any particular customer.
        Auditing every dashboard refresh would bury the entries that matter.
        """
        for name in AGGREGATE_PAGES:
            staff_client.get(reverse(name))

        assert not AuditLog.objects.filter(action=AuditAction.BACKOFFICE_VIEWED).exists()

    def test_each_visit_is_its_own_entry(self, staff_client, organization) -> None:
        """Two people looking twice is two facts, not one."""
        url = reverse("backoffice:organization-detail", args=[organization.pk])
        staff_client.get(url)
        staff_client.get(url)

        assert AuditLog.objects.filter(action=AuditAction.BACKOFFICE_VIEWED).count() == 2


class TestTheNavDoesNotAdvertiseIt:
    def test_a_customer_sees_no_backoffice_link(self, auth_client) -> None:
        """
        Hidden rather than disabled. A greyed-out "Platform" item would tell
        every customer that a cross-tenant view exists.
        """
        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert reverse("backoffice:overview") not in body

    def test_staff_do(self, staff_client, organization, staff) -> None:
        OrganizationMember.objects.create(
            organization=organization, user=staff, role=OrganizationRole.MEMBER
        )

        body = staff_client.get(reverse("dashboard:home")).content.decode()

        assert reverse("backoffice:overview") in body
