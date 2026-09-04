"""
What the backoffice shows, and what it refuses to.

The pages here are deliberately unscoped, so the interesting assertions are the
negative ones: an operator can see that a campaign ran and how many messages
failed, and cannot see a line the customer wrote or a contact's number. Support
work needs aggregates; reading correspondence is a different power.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from billing import payments
from billing.models import Subscription, SubscriptionStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(client, make_user):
    client.force_login(make_user("staff@example.com", is_staff=True))
    return client


@pytest.fixture
def two_tenants(organization, other_organization, make_contact, make_campaign):
    """Two customers with data, so "across the boundary" means something."""
    make_contact("Mine", "+97798000001", opted_in=True)
    make_contact("Also mine", "+97798000002")
    make_campaign("Spring sale")
    make_contact("Theirs", "+97798000003", organization=other_organization)
    return organization, other_organization


class TestTheOverviewCountsEverybody:
    def test_it_counts_organizations_across_tenants(
        self, staff_client, two_tenants
    ) -> None:
        response = staff_client.get(reverse("backoffice:overview"))

        assert response.context["overview"]["organizations"]["total"] == 2

    def test_it_names_nobody(self, staff_client, two_tenants) -> None:
        """
        The front page is counts. It is not audited, which is only defensible
        while it identifies no one.
        """
        mine, theirs = two_tenants

        body = staff_client.get(reverse("backoffice:overview")).content.decode()

        assert mine.name not in body
        assert theirs.name not in body

    def test_it_reports_organizations_with_no_subscription(
        self, staff_client, organization
    ) -> None:
        Subscription.objects.filter(organization=organization).delete()

        response = staff_client.get(reverse("backoffice:overview"))

        assert response.context["overview"]["subscriptions"]["missing"] == 1

    def test_outstanding_money_excludes_what_has_been_paid(
        self, staff_client, organization, on_plan, make_plan
    ) -> None:
        """An "outstanding" figure including paid invoices would be read as revenue."""
        plan = make_plan("priced", price=Decimal("29.00"))
        invoice = payments.issue(payments.generate_invoice(on_plan(organization, plan)))
        payments.collect(invoice)

        response = staff_client.get(reverse("backoffice:overview"))

        assert response.context["overview"]["money"]["outstanding_count"] == 0


class TestTheOrganizationList:
    def test_it_shows_every_tenant(self, staff_client, two_tenants) -> None:
        mine, theirs = two_tenants

        body = staff_client.get(reverse("backoffice:organizations")).content.decode()

        assert mine.name in body
        assert theirs.name in body

    def test_the_counts_are_per_organization(self, staff_client, two_tenants) -> None:
        """An annotation that counted across tenants would make every row wrong."""
        mine, theirs = two_tenants

        rows = {
            org.name: org.contact_count
            for org in staff_client.get(reverse("backoffice:organizations")).context[
                "organizations"
            ]
        }

        assert rows[mine.name] == 2
        assert rows[theirs.name] == 1

    def test_search_matches_the_owner_email(self, staff_client, two_tenants) -> None:
        mine, theirs = two_tenants

        response = staff_client.get(
            reverse("backoffice:organizations"), {"q": mine.owner.email}
        )

        names = [org.name for org in response.context["organizations"]]
        assert names == [mine.name]

    def test_search_that_matches_nothing_says_so(self, staff_client, two_tenants) -> None:
        response = staff_client.get(reverse("backoffice:organizations"), {"q": "zzzz"})

        assert "No organization matches that filter" in response.content.decode()

    def test_a_missing_subscription_is_flagged_not_blank(
        self, staff_client, organization
    ) -> None:
        Subscription.objects.filter(organization=organization).delete()

        body = staff_client.get(reverse("backoffice:organizations")).content.decode()

        assert "none" in body


class TestTheDetailPage:
    def test_it_shows_that_tenants_figures(self, staff_client, two_tenants) -> None:
        mine, _ = two_tenants

        response = staff_client.get(
            reverse("backoffice:organization-detail", args=[mine.pk])
        )

        assert response.status_code == 200
        assert response.context["counts"]["contacts"] == 2
        assert response.context["counts"]["opted_in"] == 1

    def test_it_does_not_mix_in_another_tenants_figures(
        self, staff_client, two_tenants
    ) -> None:
        """
        Unscoped queries are the point of this app and the danger of it. A count
        that quietly spanned tenants would be wrong on every page.
        """
        _, theirs = two_tenants

        response = staff_client.get(
            reverse("backoffice:organization-detail", args=[theirs.pk])
        )

        assert response.context["counts"]["contacts"] == 1

    def test_an_unknown_organization_is_a_404(self, staff_client) -> None:
        import uuid

        assert staff_client.get(
            reverse("backoffice:organization-detail", args=[uuid.uuid4()])
        ).status_code == 404

    def test_it_says_the_visit_was_recorded(self, staff_client, organization) -> None:
        """Somebody looking should know the looking is logged."""
        body = staff_client.get(
            reverse("backoffice:organization-detail", args=[organization.pk])
        ).content.decode()

        assert "has been recorded against" in body


class TestWhatItRefusesToShow:
    def test_no_message_content(
        self, staff_client, organization, make_campaign, make_contact
    ) -> None:
        """
        The line this app draws. An operator can see that a campaign ran; they
        cannot read what it said to anybody.
        """
        from messaging.models import Message, MessageStatus

        contact = make_contact("Reader", "+97798000009")
        campaign = make_campaign("Newsletter")
        Message.objects.create(
            organization=organization,
            campaign=campaign,
            contact=contact,
            to_phone_number=contact.phone_number,
            status=MessageStatus.SENT,
            rendered_payload={"body": "Your secret discount code is HUNTER2"},
        )

        body = staff_client.get(
            reverse("backoffice:organization-detail", args=[organization.pk])
        ).content.decode()

        assert "HUNTER2" not in body
        assert "secret discount" not in body

    def test_no_contact_details(self, staff_client, organization, make_contact) -> None:
        """Counts of contacts, never the contacts themselves."""
        make_contact("Bishnu Adhikari", "+97798123456")

        body = staff_client.get(
            reverse("backoffice:organization-detail", args=[organization.pk])
        ).content.decode()

        assert "+97798123456" not in body
        assert "Bishnu Adhikari" not in body

    def test_no_access_token(self, staff_client, organization) -> None:
        """The hint identifies a token. It is not the token."""
        from whatsapp.accounts import MessagingAccount, MessagingAccountStatus

        account = MessagingAccount(
            organization=organization,
            provider="meta",
            phone_number_id="pn-backoffice",
            status=MessagingAccountStatus.ACTIVE,
        )
        account.access_token = "EAAverysecrettoken1234"
        account.save()

        body = staff_client.get(
            reverse("backoffice:organization-detail", args=[organization.pk])
        ).content.decode()

        assert "EAAverysecrettoken1234" not in body
        assert account.access_token_encrypted not in body
        assert "1234" in body  # the hint is there, so an operator can match it

    def test_no_form_on_the_page_posts_to_the_backoffice(
        self, staff_client, organization
    ) -> None:
        """
        The page carries the shell's sign-out form, so the assertion is about
        where forms point rather than whether any exist.
        """
        body = staff_client.get(
            reverse("backoffice:organization-detail", args=[organization.pk])
        ).content.decode()

        assert 'action="/backoffice' not in body

    @pytest.mark.parametrize(
        "name", ["backoffice:overview", "backoffice:organizations", "backoffice:health"]
    )
    def test_every_page_refuses_a_post(self, staff_client, name: str) -> None:
        """
        Read-only by omission, asserted as behaviour rather than as markup.
        Editing belongs in Django admin, which has its own audit trail and
        permission model; duplicating it here would mean two places to get
        authorization wrong instead of one.
        """
        assert staff_client.post(reverse(name)).status_code == 405

    def test_the_detail_page_refuses_a_post_too(self, staff_client, organization) -> None:
        response = staff_client.post(
            reverse("backoffice:organization-detail", args=[organization.pk])
        )

        assert response.status_code == 405


class TestHealth:
    def test_it_lists_past_due_subscriptions(
        self, staff_client, organization, on_plan, plans
    ) -> None:
        on_plan(organization, plans["starter"], status=SubscriptionStatus.PAST_DUE)

        response = staff_client.get(reverse("backoffice:health"))

        assert organization.name in response.content.decode()

    def test_a_quiet_installation_says_so_rather_than_showing_empty_lists(
        self, staff_client, organization
    ) -> None:
        body = staff_client.get(reverse("backoffice:health")).content.decode()

        assert "Nothing past due" in body
        assert "Nothing overdue" in body

    def test_it_flags_an_organization_with_no_subscription(
        self, staff_client, organization
    ) -> None:
        Subscription.objects.filter(organization=organization).delete()

        body = staff_client.get(reverse("backoffice:health")).content.decode()

        assert "no subscription" in body.lower()
