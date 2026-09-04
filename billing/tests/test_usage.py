"""
Metering and enforcement.

Two properties carry the weight here.

**Usage is counted per organization.** A metering bug that counts across tenants
is a billing error and a data leak in one — it charges one customer for
another's traffic and, in doing so, tells them how much traffic that was.

**A campaign is refused whole or sent whole.** Never half. A send stopped at the
ceiling leaves the customer billed for a partial delivery they cannot identify
and an audience split for no reason they can see.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from billing import usage
from billing.models import Subscription, SubscriptionStatus, add_months
from billing.usage import QuotaExceeded

pytestmark = pytest.mark.django_db


@pytest.fixture
def sent_message(organization, make_campaign, make_contact):
    """A message that actually went out, which is what gets metered."""
    from messaging.models import Message, MessageStatus

    def _send(when=None, org=None, **fields):
        org = org or organization
        campaign = fields.pop("campaign", None) or make_campaign(
            f"C{Message.objects.count()}", organization=org
        )
        contact = make_contact(f"P{Message.objects.count()}", organization=org)
        return Message.objects.create(
            organization=org,
            campaign=campaign,
            contact=contact,
            to_phone_number=contact.phone_number,
            status=MessageStatus.SENT,
            sent_at=when or timezone.now(),
            **fields,
        )

    return _send


class TestCounting:
    def test_a_sent_message_counts(self, organization, sent_message) -> None:
        sent_message()
        assert usage.messages_sent(organization) == 1

    def test_an_unsent_message_does_not(self, organization, make_campaign, make_contact) -> None:
        """
        Metered on the wire, not in the queue. A message that never left cost
        the customer nothing, and billing for a campaign that failed preflight
        would be indefensible.
        """
        from messaging.models import Message, MessageStatus

        campaign = make_campaign("Queued")
        contact = make_contact("Nobody")
        Message.objects.create(
            organization=organization,
            campaign=campaign,
            contact=contact,
            to_phone_number=contact.phone_number,
            status=MessageStatus.QUEUED,
            sent_at=None,
        )

        assert usage.messages_sent(organization) == 0

    def test_another_tenants_traffic_is_not_counted(
        self, organization, other_organization, sent_message
    ) -> None:
        """A metering leak is a billing error and a data leak at once."""
        sent_message(org=organization)
        sent_message(org=other_organization)
        sent_message(org=other_organization)

        assert usage.messages_sent(organization) == 1
        assert usage.messages_sent(other_organization) == 2

    def test_last_period_does_not_count_against_this_one(
        self, organization, sent_message
    ) -> None:
        """The quota resets with the period, which is the point of having one."""
        subscription = organization.subscription
        sent_message(when=subscription.current_period_start - timezone.timedelta(days=1))
        sent_message()

        assert usage.messages_sent(organization) == 1

    def test_an_unresolved_organization_counts_nothing(self) -> None:
        """Same failure mode as the tenant scoping: None means nothing, not everything."""
        assert usage.messages_sent(None) == 0
        assert usage.contacts_held(None) == 0


class TestResolvingThePlan:
    def test_the_subscribed_plan_wins(self, organization, on_plan, plans) -> None:
        on_plan(organization, plans["business"])
        assert usage.plan_for(organization).slug == "business"

    def test_a_missing_subscription_falls_back_and_says_so(
        self, organization, caplog
    ) -> None:
        """
        Neither extreme is right. Unlimited gives the product away to anyone
        whose signup half-failed; blocked takes a working customer offline over
        a data problem they did not cause.
        """
        Subscription.objects.filter(organization=organization).delete()

        with caplog.at_level("WARNING"):
            plan = usage.plan_for(organization)

        assert plan.slug == "starter"  # the cheapest public plan
        assert "no subscription" in caplog.text

    def test_a_missing_subscription_does_not_lock_anybody_out(self, organization) -> None:
        Subscription.objects.filter(organization=organization).delete()
        assert usage.is_entitled(organization) is True


class TestEnforcement:
    def test_a_contact_over_the_limit_is_refused(
        self, organization, on_plan, make_plan, make_contact
    ) -> None:
        from contacts.services import create_contact

        make_contact("First")
        on_plan(organization, make_plan("one-contact", max_contacts=1))

        with pytest.raises(QuotaExceeded, match="contact limit is full"):
            create_contact(
                name="Second", phone_number="+9779800000002", organization=organization
            )

    def test_the_refusal_says_what_the_numbers_are(
        self, organization, on_plan, make_plan, make_contact
    ) -> None:
        """A limit message with no figures gives the customer nothing to act on."""
        from contacts.services import create_contact

        make_contact("First")
        on_plan(organization, make_plan("one-contact", max_contacts=1))

        with pytest.raises(QuotaExceeded) as caught:
            create_contact(
                name="Second", phone_number="+9779800000002", organization=organization
            )

        assert caught.value.details["used"] == 1
        assert caught.value.details["limit"] == 1
        assert "1 of 1 contacts" in caught.value.details["blockers"][0]

    def test_the_last_contact_inside_the_limit_is_allowed(
        self, organization, on_plan, make_plan
    ) -> None:
        """Off-by-one in this direction charges a customer for a contact they cannot add."""
        from contacts.services import create_contact

        on_plan(organization, make_plan("two", max_contacts=2))

        create_contact(name="A", phone_number="+9779800000011", organization=organization)
        create_contact(name="B", phone_number="+9779800000012", organization=organization)

        assert usage.contacts_held(organization) == 2

    def test_an_unlimited_plan_never_refuses(self, organization, on_plan, make_plan) -> None:
        from contacts.services import create_contact

        on_plan(organization, make_plan("boundless", max_contacts=None))

        for n in range(3):
            create_contact(
                name=f"C{n}", phone_number=f"+97798{n:08d}", organization=organization
            )

        assert usage.contacts_held(organization) == 3

    def test_quota_exceeded_renders_like_any_other_validation_error(self) -> None:
        """
        It subclasses ValidationFailed so the wizard and the REST error handler
        already know how to show it, blockers list and all.
        """
        from core.exceptions import ValidationFailed

        assert issubclass(QuotaExceeded, ValidationFailed)
        assert QuotaExceeded("x").status_code == 400


class TestSendingIsGated:
    def test_a_campaign_that_would_exceed_is_refused_whole(
        self, organization, on_plan, make_plan, sendable, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign
        from messaging.models import Message

        on_plan(organization, make_plan("tiny", max_messages_per_month=1))

        with pytest.raises(QuotaExceeded, match="monthly message limit"):
            launch_campaign(sendable, user=organization.owner)

        assert Message.objects.filter(campaign=sendable).count() == 0

    def test_the_campaign_stays_where_it_was(
        self, organization, on_plan, make_plan, sendable, recording_dispatcher
    ) -> None:
        """A refused launch must not leave a campaign stuck in PROCESSING."""
        from campaigns.models import CampaignStatus
        from campaigns.services import launch_campaign

        before = sendable.status
        on_plan(organization, make_plan("tiny", max_messages_per_month=0))

        with pytest.raises(QuotaExceeded):
            launch_campaign(sendable, user=organization.owner)

        sendable.refresh_from_db()
        assert sendable.status == before
        assert sendable.status != CampaignStatus.PROCESSING

    def test_a_campaign_inside_the_limit_goes(
        self, organization, on_plan, make_plan, sendable, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        on_plan(organization, make_plan("roomy", max_messages_per_month=100))

        launch_campaign(sendable, user=organization.owner)  # does not raise

    def test_a_cancelled_subscription_cannot_send(
        self, organization, on_plan, plans, sendable, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        on_plan(organization, plans["self-hosted"], status=SubscriptionStatus.CANCELED)

        with pytest.raises(QuotaExceeded, match="not active"):
            launch_campaign(sendable, user=organization.owner)

    def test_lapsed_and_over_quota_are_different_messages(
        self, organization, on_plan, plans, sendable, recording_dispatcher
    ) -> None:
        """
        They need different remedies — one needs paying, the other needs waiting
        or upgrading — so collapsing them would send half the customers to the
        wrong page.
        """
        from campaigns.services import launch_campaign

        on_plan(organization, plans["self-hosted"], status=SubscriptionStatus.EXPIRED)

        with pytest.raises(QuotaExceeded) as caught:
            launch_campaign(sendable, user=organization.owner)

        assert "upgrade" not in str(caught.value).lower()
        assert caught.value.details["status"] == SubscriptionStatus.EXPIRED

    def test_a_past_due_subscription_still_sends(
        self, organization, on_plan, plans, sendable, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        on_plan(organization, plans["self-hosted"], status=SubscriptionStatus.PAST_DUE)

        launch_campaign(sendable, user=organization.owner)  # does not raise


class TestTheSummary:
    def test_it_reports_each_metric(self, organization, on_plan, make_plan) -> None:
        on_plan(organization, make_plan("measured", max_contacts=10))

        metrics = usage.summary(organization)["metrics"]

        assert metrics["max_contacts"]["limit"] == 10
        assert metrics["max_contacts"]["remaining"] == 10
        assert metrics["max_contacts"]["percent"] == 0

    def test_an_unlimited_metric_has_no_percentage(
        self, organization, on_plan, make_plan
    ) -> None:
        """A progress bar against no ceiling is meaningless; 0% would imply one."""
        on_plan(organization, make_plan("boundless"))

        contacts = usage.summary(organization)["metrics"]["max_contacts"]

        assert contacts["limit"] is None
        assert contacts["percent"] is None
        assert contacts["remaining"] is None

    def test_being_over_a_ceiling_is_reported_rather_than_hidden(
        self, organization, on_plan, make_plan, make_contact
    ) -> None:
        """A customer can end up over a limit by being moved down a plan."""
        make_contact("A")
        make_contact("B")
        on_plan(organization, make_plan("one", max_contacts=1))

        contacts = usage.summary(organization)["metrics"]["max_contacts"]

        assert contacts["exceeded"] is True
        assert contacts["percent"] == 100  # clamped, not 200


class TestClosingAPeriod:
    def test_it_freezes_the_total_and_moves_the_period(
        self, organization, sent_message
    ) -> None:
        subscription = organization.subscription
        sent_message()
        started_at = subscription.current_period_start

        snapshot, created = usage.close_period(subscription)
        subscription.refresh_from_db()

        assert created and snapshot.messages_sent == 1
        assert subscription.current_period_start > started_at

    def test_closing_the_same_period_twice_does_not_double_count(
        self, organization, sent_message
    ) -> None:
        """
        Billing jobs get retried. A retry that arrives after the snapshot was
        written but before the period moved must not bill the month again.
        """
        from billing.models import UsageSnapshot

        subscription = organization.subscription
        started, ended = subscription.current_period_start, subscription.current_period_end
        sent_message()
        usage.close_period(subscription)

        # What a retry sees: the period it already closed, still current.
        subscription.current_period_start = started
        subscription.current_period_end = ended
        subscription.save()

        _, created = usage.close_period(subscription)

        assert created is False
        assert UsageSnapshot.objects.filter(organization=organization).count() == 1

    def test_a_trial_becomes_active_when_it_runs_out(self, organization, on_plan, plans) -> None:
        subscription = on_plan(
            organization, plans["starter"], status=SubscriptionStatus.TRIALING
        )

        usage.close_period(subscription)
        subscription.refresh_from_db()

        assert subscription.status == SubscriptionStatus.ACTIVE

    def test_a_pending_cancellation_takes_effect(self, organization, on_plan, plans) -> None:
        """The customer keeps the time they paid for, then it stops."""
        subscription = on_plan(
            organization, plans["starter"], cancel_at_period_end=True
        )

        usage.close_period(subscription)
        subscription.refresh_from_db()

        assert subscription.status == SubscriptionStatus.CANCELED
        assert subscription.canceled_at is not None

    def test_the_snapshot_survives_the_messages_it_counted(
        self, organization, sent_message
    ) -> None:
        """Retention deletes messages; an invoice cannot be re-derived afterwards."""
        from messaging.models import Message

        sent_message()
        snapshot, _ = usage.close_period(organization.subscription)

        Message.objects.all().delete()
        snapshot.refresh_from_db()

        assert snapshot.messages_sent == 1


class TestTheRolloverTask:
    def test_it_closes_what_is_due_and_leaves_the_rest(
        self, organization, other_organization
    ) -> None:
        from billing.tasks import roll_billing_periods

        overdue = organization.subscription
        Subscription.objects.filter(pk=overdue.pk).update(
            current_period_start=add_months(timezone.now(), -2),
            current_period_end=add_months(timezone.now(), -1),
        )

        result = roll_billing_periods()

        overdue.refresh_from_db()
        assert result["closed"] >= 1
        assert overdue.current_period_end > timezone.now()

    def test_a_long_gap_is_caught_up_period_by_period(self, organization) -> None:
        """
        Rolling straight to now would lose the periods in between along with
        their usage, which is the history an invoice is written from.
        """
        from billing.models import UsageSnapshot
        from billing.tasks import roll_billing_periods

        Subscription.objects.filter(organization=organization).update(
            current_period_start=add_months(timezone.now(), -3),
            current_period_end=add_months(timezone.now(), -2),
        )

        roll_billing_periods()

        assert UsageSnapshot.objects.filter(organization=organization).count() >= 2

    def test_one_bad_subscription_does_not_stop_the_others(
        self, organization, monkeypatch
    ) -> None:
        from billing import tasks

        Subscription.objects.filter(organization=organization).update(
            current_period_start=add_months(timezone.now(), -2),
            current_period_end=add_months(timezone.now(), -1),
        )

        def explode(subscription):
            raise RuntimeError("bad data")

        monkeypatch.setattr(tasks, "close_period", explode)

        assert tasks.roll_billing_periods()["failed"] == 1
