"""
Starting, changing and ending a subscription.

Everything that writes a subscription goes through ``billing.services``, so
these tests are also what guarantees a plan cannot move without an audit entry.
A limit that changed with no trace is indistinguishable from a limit that was
never enforced.
"""

from __future__ import annotations

import pytest

from billing import services
from billing.models import Plan, Subscription, SubscriptionStatus
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


class TestSubscribing:
    def test_a_new_organization_starts_on_the_cheapest_public_plan(
        self, other_organization
    ) -> None:
        Subscription.objects.filter(organization=other_organization).delete()

        subscription = services.subscribe(other_organization)

        assert subscription.plan.slug == "starter"

    def test_a_plan_with_a_trial_starts_trialing(self, other_organization, plans) -> None:
        Subscription.objects.filter(organization=other_organization).delete()

        subscription = services.subscribe(other_organization, plans["starter"])

        assert subscription.status == SubscriptionStatus.TRIALING
        assert subscription.trial_end is not None

    def test_a_plan_without_one_starts_active(self, other_organization, plans) -> None:
        Subscription.objects.filter(organization=other_organization).delete()

        subscription = services.subscribe(other_organization, plans["self-hosted"])

        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.trial_end is None

    def test_subscribing_twice_replaces_rather_than_duplicates(
        self, organization, plans
    ) -> None:
        """Two rows disagreeing about a customer's limits is worth making impossible."""
        services.subscribe(organization, plans["starter"])
        services.subscribe(organization, plans["business"])

        # Queried rather than read off the instance: the fixture cached the
        # reverse relation before either call replaced the row behind it.
        subscriptions = Subscription.objects.filter(organization=organization)
        assert subscriptions.count() == 1
        assert subscriptions.get().plan.slug == "business"

    def test_it_is_audited(self, other_organization) -> None:
        Subscription.objects.filter(organization=other_organization).delete()

        services.subscribe(other_organization)

        assert AuditLog.objects.filter(action=AuditAction.SUBSCRIPTION_STARTED).exists()

    def test_an_empty_catalogue_is_refused_rather_than_guessed_at(
        self, other_organization
    ) -> None:
        from core.exceptions import ValidationFailed

        Subscription.objects.all().delete()
        Plan.objects.all().delete()

        with pytest.raises(ValidationFailed, match="no plans"):
            services.subscribe(other_organization)


class TestChangingPlan:
    def test_the_new_limits_apply_immediately(self, organization, plans) -> None:
        """An upgrade is usually bought by somebody who has just hit a ceiling."""
        services.subscribe(organization, plans["starter"])

        services.change_plan(organization.subscription, plans["business"])

        assert Subscription.objects.get(organization=organization).plan.max_contacts == 10_000

    def test_the_period_is_not_restarted(self, organization, plans) -> None:
        """
        Otherwise switching plans hands out a fresh month of quota, which is a
        free-messages exploit with an obvious recipe.
        """
        subscription = services.subscribe(organization, plans["starter"])
        started_at = subscription.current_period_start

        services.change_plan(subscription, plans["business"])
        subscription.refresh_from_db()

        assert subscription.current_period_start == started_at

    def test_changing_to_the_same_plan_does_nothing(self, organization, plans) -> None:
        subscription = services.subscribe(organization, plans["starter"])
        before = AuditLog.objects.count()

        services.change_plan(subscription, plans["starter"])

        assert AuditLog.objects.count() == before

    def test_it_is_audited_with_both_ends(self, organization, plans) -> None:
        subscription = services.subscribe(organization, plans["starter"])

        services.change_plan(subscription, plans["business"])

        entry = AuditLog.objects.filter(action=AuditAction.SUBSCRIPTION_CHANGED).latest(
            "created_at"
        )
        assert entry.metadata["from"] == "starter"
        assert entry.metadata["to"] == "business"


class TestCancelling:
    def test_by_default_it_runs_to_the_end_of_the_period(self, organization) -> None:
        """Cutting somebody off on the click takes away time they have paid for."""
        subscription = organization.subscription

        services.cancel(subscription)

        assert subscription.cancel_at_period_end is True
        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.is_entitled

    def test_immediately_means_immediately(self, organization) -> None:
        subscription = organization.subscription

        services.cancel(subscription, immediately=True)

        assert subscription.status == SubscriptionStatus.CANCELED
        assert subscription.canceled_at is not None
        assert not subscription.is_entitled

    def test_a_pending_cancellation_can_be_undone(self, organization) -> None:
        subscription = organization.subscription
        services.cancel(subscription)

        services.resume(subscription)

        assert subscription.cancel_at_period_end is False

    def test_resuming_something_that_was_not_cancelled_is_a_no_op(
        self, organization
    ) -> None:
        subscription = organization.subscription
        before = AuditLog.objects.count()

        services.resume(subscription)

        assert AuditLog.objects.count() == before

    def test_cancelling_is_audited(self, organization) -> None:
        services.cancel(organization.subscription, immediately=True)

        assert AuditLog.objects.filter(action=AuditAction.SUBSCRIPTION_CANCELLED).exists()


class TestRegistrationSubscribes:
    def test_signing_up_creates_a_subscription(self, client) -> None:
        """
        The fourth thing registration creates. An organization without one has
        no limits to check against, and billing.usage has to guess every time.
        """
        from organizations.models import Organization

        client.post(
            "/accounts/register/",
            {
                "organization_name": "Sherpa Logistics",
                "first_name": "Pemba",
                "last_name": "Sherpa",
                "email": "pemba@example.com",
                "phone": "",
                "password1": "correct-horse-battery-staple",
                "password2": "correct-horse-battery-staple",
            },
        )

        organization = Organization.objects.get(name="Sherpa Logistics")
        assert organization.subscription.plan.slug == "starter"
        assert organization.subscription.status == SubscriptionStatus.TRIALING

    def test_a_failed_registration_leaves_no_subscription(self, client) -> None:
        """It is inside the same transaction as the account and the organization."""
        client.post(
            "/accounts/register/",
            {
                "organization_name": "Never Created",
                "first_name": "X",
                "last_name": "Y",
                "email": "x@example.com",
                "phone": "",
                "password1": "correct-horse-battery-staple",
                "password2": "does-not-match",
            },
        )

        assert not Subscription.objects.filter(
            organization__name="Never Created"
        ).exists()
