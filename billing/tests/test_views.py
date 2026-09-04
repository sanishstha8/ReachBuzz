"""
The customer-facing billing pages.

Most of what matters here is refusal, not rendering:

* a member can *read* the bill but not change it;
* one organization can never see another's invoice, by URL or otherwise;
* a plan change is a POST, because a GET that changes what somebody pays would
  be followed by every link prefetcher and scanner that saw it;
* a downgrade that would not fit is refused with the numbers rather than
  accepted and discovered later.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from billing import payments
from billing.invoicing import Invoice
from billing.models import Subscription, SubscriptionStatus
from organizations.models import OrganizationMember, OrganizationRole

pytestmark = pytest.mark.django_db


@pytest.fixture
def priced(make_plan):
    return make_plan("priced", price=Decimal("29.00"), currency="USD", is_public=True)


@pytest.fixture
def invoice(organization, on_plan, priced):
    return payments.issue(payments.generate_invoice(on_plan(organization, priced)))


def demote(organization, user, role=OrganizationRole.MEMBER):
    OrganizationMember.objects.filter(organization=organization, user=user).update(role=role)


class TestWhoCanSeeIt:
    def test_an_anonymous_visitor_is_sent_to_sign_in(self, client) -> None:
        response = client.get(reverse("billing:overview"))

        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_an_ordinary_member_can_read_the_bill(
        self, auth_client, organization, operator
    ) -> None:
        """Hiding what the product costs from the people using it helps nobody."""
        demote(organization, operator)

        response = auth_client.get(reverse("billing:overview"))

        assert response.status_code == 200
        assert response.context["can_manage_billing"] is False

    def test_an_owner_can_manage(self, auth_client) -> None:
        response = auth_client.get(reverse("billing:overview"))

        assert response.context["can_manage_billing"] is True

    def test_an_administrator_can_too(self, auth_client, organization, operator) -> None:
        demote(organization, operator, OrganizationRole.ADMIN)

        assert auth_client.get(reverse("billing:overview")).context["can_manage_billing"] is True


class TestChangingThePlanNeedsARole:
    def test_a_member_cannot_change_the_plan(
        self, auth_client, organization, operator, priced
    ) -> None:
        demote(organization, operator)
        before = Subscription.objects.get(organization=organization).plan_id

        response = auth_client.post(reverse("billing:change-plan", args=[priced.slug]))

        assert response.status_code == 302
        assert Subscription.objects.get(organization=organization).plan_id == before

    def test_a_member_cannot_cancel(self, auth_client, organization, operator) -> None:
        demote(organization, operator)

        auth_client.post(reverse("billing:cancel"))

        assert Subscription.objects.get(organization=organization).cancel_at_period_end is False

    def test_an_owner_can(self, auth_client, organization, priced) -> None:
        auth_client.post(reverse("billing:change-plan", args=[priced.slug]))

        assert Subscription.objects.get(organization=organization).plan_id == priced.pk

    def test_a_get_does_not_change_anything(
        self, auth_client, organization, priced
    ) -> None:
        """
        A plan-change link would be followed by every prefetcher that saw it.
        405, not a redirect, so the mistake is loud if anyone ever adds one.
        """
        before = Subscription.objects.get(organization=organization).plan_id

        response = auth_client.get(reverse("billing:change-plan", args=[priced.slug]))

        assert response.status_code == 405
        assert Subscription.objects.get(organization=organization).plan_id == before

    def test_an_unknown_plan_is_a_404(self, auth_client) -> None:
        assert auth_client.post(reverse("billing:change-plan", args=["no-such"])).status_code == 404

    def test_a_private_plan_cannot_be_chosen_by_url(
        self, auth_client, organization, make_plan
    ) -> None:
        """`is_active=False` means nobody may subscribe, guessed slug or not."""
        hidden = make_plan("retired", is_public=False, is_active=False)

        assert auth_client.post(
            reverse("billing:change-plan", args=[hidden.slug])
        ).status_code == 404


class TestDowngradesThatWouldNotFit:
    def test_it_is_refused_with_the_numbers(
        self, auth_client, organization, make_contact, make_plan
    ) -> None:
        for n in range(3):
            make_contact(f"C{n}", f"+97798{n:08d}")
        tiny = make_plan("tiny", max_contacts=1, is_public=True)
        before = Subscription.objects.get(organization=organization).plan_id

        response = auth_client.post(
            reverse("billing:change-plan", args=[tiny.slug]), follow=True
        )

        assert Subscription.objects.get(organization=organization).plan_id == before
        body = response.content.decode()
        assert "allows 1 contacts and you have 3" in body

    def test_a_downgrade_that_does_fit_is_allowed(
        self, auth_client, organization, make_contact, make_plan
    ) -> None:
        """The question is whether what they have fits, not whether it is smaller."""
        make_contact("Only", "+97798000001")
        roomy = make_plan("roomy", max_contacts=10, is_public=True)

        auth_client.post(reverse("billing:change-plan", args=[roomy.slug]))

        assert Subscription.objects.get(organization=organization).plan_id == roomy.pk


class TestCancelAndResume:
    def test_cancelling_runs_to_the_end_of_the_period(
        self, auth_client, organization
    ) -> None:
        auth_client.post(reverse("billing:cancel"))

        subscription = Subscription.objects.get(organization=organization)
        assert subscription.cancel_at_period_end is True
        assert subscription.status == SubscriptionStatus.ACTIVE, "access must not stop today"

    def test_the_overview_offers_to_undo_it(self, auth_client, organization) -> None:
        auth_client.post(reverse("billing:cancel"))

        body = auth_client.get(reverse("billing:overview")).content.decode()

        assert "Keep my subscription" in body

    def test_resuming_undoes_it(self, auth_client, organization) -> None:
        auth_client.post(reverse("billing:cancel"))

        auth_client.post(reverse("billing:resume"))

        assert Subscription.objects.get(organization=organization).cancel_at_period_end is False


class TestInvoicesAreScoped:
    def test_another_organizations_invoice_is_a_404(
        self, auth_client, other_organization, on_plan, priced
    ) -> None:
        """
        Not a 403. Telling somebody an invoice exists but is not theirs confirms
        it exists, and an invoice carries a business name and an amount.
        """
        theirs = payments.issue(payments.generate_invoice(on_plan(other_organization, priced)))

        assert auth_client.get(
            reverse("billing:invoice-detail", args=[theirs.pk])
        ).status_code == 404

    def test_the_list_shows_only_this_organizations(
        self, auth_client, organization, other_organization, on_plan, priced, make_plan
    ) -> None:
        mine = payments.issue(payments.generate_invoice(on_plan(organization, priced)))
        theirs = payments.issue(
            payments.generate_invoice(on_plan(other_organization, make_plan("p2", price=Decimal("9.00"))))
        )

        body = auth_client.get(reverse("billing:invoices")).content.decode()

        assert mine.number in body
        assert theirs.number not in body

    def test_my_own_invoice_renders(self, auth_client, invoice) -> None:
        response = auth_client.get(reverse("billing:invoice-detail", args=[invoice.pk]))

        assert response.status_code == 200
        assert invoice.number in response.content.decode()

    def test_failed_attempts_are_shown_not_hidden(
        self, auth_client, invoice, settings
    ) -> None:
        """"We tried twice" is a fact a customer may need explained."""
        settings.MOCK_PAYMENT_FAILURE_RATE = 1.0
        payments.collect(invoice)

        body = auth_client.get(reverse("billing:invoice-detail", args=[invoice.pk])).content.decode()

        assert "Payment attempts" in body
        assert "Failed" in body


class TestItDoesNotInventThings:
    def test_an_unpriced_plan_says_pricing_on_request(self, auth_client, plans, on_plan, organization) -> None:
        on_plan(organization, plans["starter"])

        body = auth_client.get(reverse("billing:overview")).content.decode()

        assert "Pricing on request" in body

    def test_an_unlimited_metric_gets_no_progress_bar(
        self, auth_client, organization, on_plan, make_plan
    ) -> None:
        on_plan(organization, make_plan("boundless"))

        body = auth_client.get(reverse("billing:overview")).content.decode()

        assert "of unlimited" in body
        assert 'role="progressbar"' not in body

    def test_a_limited_metric_does_get_one(
        self, auth_client, organization, on_plan, make_plan
    ) -> None:
        on_plan(organization, make_plan("bounded", max_contacts=100))

        body = auth_client.get(reverse("billing:overview")).content.decode()

        assert 'role="progressbar"' in body

    def test_no_invoices_explains_why(self, auth_client, plans, on_plan, organization) -> None:
        """An empty table styled like a real one reads as a bug."""
        on_plan(organization, plans["starter"])

        body = auth_client.get(reverse("billing:invoices")).content.decode()

        assert "quoted individually" in body

    def test_being_over_a_limit_states_the_overage(
        self, auth_client, organization, make_contact, on_plan, make_plan
    ) -> None:
        for n in range(3):
            make_contact(f"C{n}", f"+97798{n:08d}")
        on_plan(organization, make_plan("one", max_contacts=1))

        body = auth_client.get(reverse("billing:overview")).content.decode()

        assert "Over the limit by 2" in body


class TestThePagesRender:
    @pytest.mark.parametrize("name", ["overview", "plans", "invoices"])
    def test_it_returns_200(self, auth_client, name: str) -> None:
        assert auth_client.get(reverse(f"billing:{name}")).status_code == 200

    def test_the_nav_links_to_billing(self, auth_client) -> None:
        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert reverse("billing:overview") in body

    def test_an_organization_with_no_subscription_still_renders(
        self, auth_client, organization
    ) -> None:
        """A page that 500s when a row is missing is worse than one that explains."""
        Subscription.objects.filter(organization=organization).delete()

        response = auth_client.get(reverse("billing:overview"))

        assert response.status_code == 200
        assert "no subscription on record" in response.content.decode()

    def test_no_invoice_of_another_tenant_leaks_into_the_overview(
        self, auth_client, other_organization, on_plan, priced
    ) -> None:
        theirs = payments.issue(payments.generate_invoice(on_plan(other_organization, priced)))

        body = auth_client.get(reverse("billing:overview")).content.decode()

        assert theirs.number not in body
        assert Invoice.objects.count() == 1
