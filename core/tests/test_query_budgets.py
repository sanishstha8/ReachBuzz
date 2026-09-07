"""
Pages must not get slower as customers arrive.

Measured before optimising, and every page in Stages 3 to 9 already came out
flat — the annotations in the backoffice and the ``select_related`` in billing
did their job. So this file is not a fix; it is the thing that keeps it true.

**These are budgets, not measurements.** The numbers are deliberately loose. A
test that fails because somebody added one legitimate query is a test people
raise the number on without reading, and then it is protecting nothing. What
each budget catches is an N+1: not "this got slightly heavier" but "this now
grows with the number of rows on the page".

The scaling tests are the sharper half. A page can be within budget and still be
quadratic; only the same page measured against two data sizes shows that.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from billing.services import subscribe
from contacts.models import Contact
from organizations.models import Organization, OrganizationMember, OrganizationRole

pytestmark = pytest.mark.django_db

#: Page -> the most queries it may take. Roughly double what each does today.
BUDGETS = {
    "billing:overview": 40,
    "billing:plans": 40,
    "billing:invoices": 30,
    "backoffice:overview": 60,
    "backoffice:organizations": 30,
    "backoffice:health": 40,
    "dashboard:home": 40,
}


@pytest.fixture
def staff_client(client, make_user):
    client.force_login(make_user("staff@example.com", is_staff=True))
    return client


def queries_for(client, url: str) -> int:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200, f"{url} returned {response.status_code}"
    return len(captured)


def add_organizations(count: int, *, contacts_each: int = 2):
    """A handful of other customers, each with a little data."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    made = []

    for index in range(count):
        user = User.objects.create_user(
            email=f"scale{index}@example.com", password="x", email_verified=True
        )
        organization = Organization.objects.create(name=f"Scale {index}", owner=user)
        OrganizationMember.objects.create(
            organization=organization, user=user, role=OrganizationRole.OWNER
        )
        subscribe(organization)
        for contact in range(contacts_each):
            Contact.objects.create(
                organization=organization,
                name=f"C{index}-{contact}",
                phone_number=f"+977987{index:03d}{contact:02d}",
                country_code="977",
            )
        made.append(organization)

    return made


class TestEachPageIsWithinBudget:
    @pytest.mark.parametrize("name", sorted(BUDGETS))
    def test_it_does_not_exceed_its_budget(self, staff_client, organization, name: str) -> None:
        budget = BUDGETS[name]
        used = queries_for(staff_client, reverse(name))

        assert used <= budget, (
            f"{name} used {used} queries against a budget of {budget}. If this is a "
            "legitimate new query, raise the budget; if the number moved a lot, it "
            "is probably an N+1."
        )


class TestNothingScalesWithTheNumberOfCustomers:
    """
    The half that actually catches an N+1.

    Each page is measured with one organization and then with seven. A flat
    count means the page is doing set-based work; a count that grew by roughly
    the number of rows means it is querying per row.
    """

    @pytest.mark.parametrize(
        "name", ["backoffice:overview", "backoffice:organizations", "backoffice:health"]
    )
    def test_the_cross_tenant_pages_stay_flat(self, staff_client, organization, name: str) -> None:
        url = reverse(name)
        before = queries_for(staff_client, url)

        add_organizations(6)
        after = queries_for(staff_client, url)

        assert after <= before + 2, (
            f"{name} went from {before} to {after} queries when six organizations "
            "were added, which is an N+1 over organizations."
        )

    def test_the_organization_list_stays_flat_with_contacts(
        self, staff_client, organization
    ) -> None:
        """The counts are annotated; fetching them per row would show up here."""
        url = reverse("backoffice:organizations")
        before = queries_for(staff_client, url)

        add_organizations(6, contacts_each=20)
        after = queries_for(staff_client, url)

        assert after <= before + 2

    def test_the_invoice_list_stays_flat(
        self, staff_client, organization, on_plan, make_plan
    ) -> None:
        from decimal import Decimal

        from billing import payments

        url = reverse("billing:invoices")
        before = queries_for(staff_client, url)

        subscription = on_plan(organization, make_plan("priced", price=Decimal("29.00")))
        for _ in range(5):
            invoice = payments.generate_invoice(subscription)
            if invoice is not None:
                payments.issue(invoice)
            # Roll the period so the next invoice is for a different one; the
            # unique constraint is per (organization, period_start).
            subscription.current_period_start = subscription.current_period_end
            subscription.current_period_end = subscription.next_period_end(
                subscription.current_period_start
            )
            subscription.save()

        after = queries_for(staff_client, url)

        assert after <= before + 2, "the invoice list is querying per invoice"


class TestTheDashboardStaysFlatWithMessages:
    def test_adding_messages_does_not_add_queries(
        self, staff_client, organization, make_campaign, make_contact
    ) -> None:
        """
        The dashboard aggregates. If it ever starts iterating, this is where it
        shows up — and it is the page every customer loads first.
        """
        from messaging.models import Message, MessageStatus

        url = reverse("dashboard:home")
        before = queries_for(staff_client, url)

        campaign = make_campaign("Busy")
        for index in range(25):
            contact = make_contact(f"P{index}", f"+977986{index:08d}")
            Message.objects.create(
                organization=organization,
                campaign=campaign,
                contact=contact,
                to_phone_number=contact.phone_number,
                status=MessageStatus.SENT,
            )

        after = queries_for(staff_client, url)

        assert after <= before + 2
