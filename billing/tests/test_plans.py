"""
Plans and what they permit.

The distinction these tests exist to protect is between a limit of ``None``
(unlimited) and a limit of ``0`` (none at all). They look alike in a database
row and behave in opposite ways, and getting them confused means either giving
the product away or locking every customer out.
"""

from __future__ import annotations

import pytest

from billing.models import Plan, SubscriptionStatus, add_months

pytestmark = pytest.mark.django_db


class TestLimits:
    def test_an_empty_limit_means_unlimited(self, make_plan) -> None:
        plan = make_plan("boundless", max_contacts=None)

        assert plan.allows("max_contacts", current=10_000_000, additional=1)
        assert plan.remaining("max_contacts", current=5) is None

    def test_a_zero_limit_means_none_at_all(self, make_plan) -> None:
        """Not the same as unlimited, however similar the two look in a row."""
        plan = make_plan("frozen", max_contacts=0)

        assert not plan.allows("max_contacts", current=0, additional=1)
        assert plan.remaining("max_contacts", current=0) == 0

    def test_the_check_includes_what_is_about_to_be_added(self, make_plan) -> None:
        """
        The whole reason the size of the work is passed in. A campaign to 900
        recipients against 200 remaining must be refused before it starts, not
        discovered halfway through.
        """
        plan = make_plan("thousand", max_messages_per_month=1000)

        assert plan.allows("max_messages_per_month", current=800, additional=200)
        assert not plan.allows("max_messages_per_month", current=800, additional=201)

    def test_remaining_never_goes_negative(self, make_plan) -> None:
        """A customer over their limit has zero left, not minus fifty."""
        plan = make_plan("small", max_contacts=100)

        assert plan.remaining("max_contacts", current=150) == 0

    def test_an_unknown_metric_is_a_programming_error(self, make_plan) -> None:
        """Typo a metric name and the answer must not be a silent 'allowed'."""
        plan = make_plan("any")

        with pytest.raises(ValueError, match="not a plan limit"):
            plan.allows("max_bananas", current=0)


class TestTheSeededCatalogue:
    def test_the_three_advertised_tiers_exist(self, plans) -> None:
        assert {"starter", "business", "self-hosted"} <= set(plans)

    def test_no_price_was_invented(self, plans) -> None:
        """
        A migration is not the place to decide what the product costs. The
        pricing page renders "Pricing on request" until somebody sets a figure.
        """
        assert all(plan.price is None for plan in plans.values())

    def test_the_contact_limits_match_what_the_page_advertises(self, plans) -> None:
        assert plans["starter"].max_contacts == 1000
        assert plans["business"].max_contacts == 10_000
        assert plans["self-hosted"].max_contacts is None

    def test_no_message_limit_was_invented_either(self, plans) -> None:
        """
        A monthly cap was never advertised, so none is imposed. The enforcement
        is built and tested; the number is a business decision, not a migration.
        """
        assert all(plan.max_messages_per_month is None for plan in plans.values())

    def test_seeding_twice_does_not_duplicate(self) -> None:
        """Restoring a snapshot and migrating it forward re-runs this migration."""
        from importlib import import_module

        from django.apps import apps

        migration = import_module("billing.migrations.0002_seed_plans")
        before = Plan.objects.count()

        migration.seed_plans(apps, None)

        assert Plan.objects.count() == before

    def test_re_seeding_does_not_overwrite_an_edited_limit(self) -> None:
        """An operator who raised a ceiling must not have it reset by a migration."""
        from importlib import import_module

        from django.apps import apps

        Plan.objects.filter(slug="starter").update(max_contacts=99_999)
        import_module("billing.migrations.0002_seed_plans").seed_plans(apps, None)

        assert Plan.objects.get(slug="starter").max_contacts == 99_999

    def test_the_slug_fills_itself_in(self) -> None:
        assert Plan.objects.create(name="Growth Plus").slug == "growth-plus"


class TestPeriodArithmetic:
    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            ("2026-01-31", "2026-02-28"),  # clamps into a short month
            ("2026-01-15", "2026-02-15"),
            ("2026-12-31", "2027-01-31"),  # crosses the year
            ("2024-01-31", "2024-02-29"),  # a leap year is not February 28
        ],
    )
    def test_a_month_later_is_a_real_date(self, start: str, expected: str) -> None:
        """31 January plus one month is 28 February, not the 3rd of March."""
        from datetime import datetime

        moment = datetime.fromisoformat(start)
        assert add_months(moment, 1).date().isoformat() == expected

    def test_a_year_later_survives_the_29th(self) -> None:
        from datetime import datetime

        assert add_months(datetime(2024, 2, 29), 12).date().isoformat() == "2025-02-28"


class TestSubscriptionShape:
    def test_the_period_end_fills_itself_in_from_the_plan(self, organization, make_plan) -> None:
        from billing.models import Subscription

        Subscription.objects.filter(organization=organization).delete()
        subscription = Subscription.objects.create(
            organization=organization, plan=make_plan("monthly")
        )

        assert subscription.current_period_end > subscription.current_period_start

    def test_a_yearly_plan_gets_a_yearly_period(self, organization, make_plan) -> None:
        from billing.models import PlanInterval, Subscription

        Subscription.objects.filter(organization=organization).delete()
        plan = make_plan("annual", interval=PlanInterval.YEARLY)
        subscription = Subscription.objects.create(organization=organization, plan=plan)

        delta = subscription.current_period_end - subscription.current_period_start
        assert 360 < delta.days < 370

    def test_a_backwards_period_is_refused_by_the_database(
        self, organization, make_plan
    ) -> None:
        """A constraint rather than a check in Python: bad data has other routes in."""
        from django.db import IntegrityError, transaction

        from billing.models import Subscription

        subscription = organization.subscription
        with pytest.raises(IntegrityError), transaction.atomic():
            Subscription.objects.filter(pk=subscription.pk).update(
                current_period_end=subscription.current_period_start
            )

    def test_past_due_still_entitles(self, organization, on_plan, plans) -> None:
        """
        A failed card starts a conversation; it does not sever a business's
        messaging mid-campaign. Stage 4 decides how long the grace lasts.
        """
        subscription = on_plan(
            organization, plans["starter"], status=SubscriptionStatus.PAST_DUE
        )
        assert subscription.is_entitled

    def test_cancelled_does_not(self, organization, on_plan, plans) -> None:
        subscription = on_plan(
            organization, plans["starter"], status=SubscriptionStatus.CANCELED
        )
        assert not subscription.is_entitled
