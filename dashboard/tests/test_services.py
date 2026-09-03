"""
The reporting aggregates.

The tests that matter most here are the ones about *meaning*: that the status
buckets partition the messages rather than overlapping, that a quiet day is
present as a zero rather than closed up, and that a delivery rate is measured
against what the provider accepted rather than against everything queued. Each
of those is a number an operator would act on, and each would look plausible if
it were wrong.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from campaigns.models import CampaignStatus
from dashboard import services
from messaging.models import MessageStatus

pytestmark = pytest.mark.django_db


class TestReportPeriod:
    def test_a_period_is_inclusive_of_both_ends(self) -> None:
        period = services.ReportPeriod(start=date(2026, 8, 1), end=date(2026, 8, 7))
        assert period.days == 7
        assert len(period.dates()) == 7

    def test_the_upper_bound_is_exclusive_midnight_of_the_next_day(self) -> None:
        period = services.ReportPeriod(start=date(2026, 8, 1), end=date(2026, 8, 1))
        assert period.end_at - period.start_at == timedelta(days=1)

    def test_a_reversed_period_is_refused(self) -> None:
        with pytest.raises(ValueError):
            services.ReportPeriod(start=date(2026, 8, 7), end=date(2026, 8, 1))

    def test_last_days_ends_today(self) -> None:
        period = services.ReportPeriod.last_days(7)
        assert period.end == timezone.localdate()
        assert period.days == 7

    def test_an_absurd_length_is_capped(self) -> None:
        assert services.ReportPeriod.last_days(10_000).days == services.MAX_PERIOD_DAYS


class TestResolvePeriod:
    def test_no_parameters_gives_the_default_window(self) -> None:
        assert services.resolve_period({}).days == services.DEFAULT_PERIOD_DAYS

    def test_days_parameter_is_honoured(self) -> None:
        assert services.resolve_period({"days": "7"}).days == 7

    def test_explicit_dates_are_honoured(self) -> None:
        period = services.resolve_period({"start": "2026-08-01", "end": "2026-08-10"})
        assert (period.start, period.end) == (date(2026, 8, 1), date(2026, 8, 10))

    def test_explicit_dates_win_over_days(self) -> None:
        period = services.resolve_period({"days": "90", "start": "2026-08-01", "end": "2026-08-02"})
        assert period.days == 2

    def test_a_hand_edited_url_falls_back_rather_than_raising(self) -> None:
        """A report page must not 500 because someone typed in the address bar."""
        assert services.resolve_period({"days": "not-a-number"}).days == services.DEFAULT_PERIOD_DAYS
        assert services.resolve_period({"start": "yesterday"}).days == services.DEFAULT_PERIOD_DAYS

    def test_a_reversed_range_falls_back_instead_of_raising(self) -> None:
        period = services.resolve_period({"start": "2026-08-10", "end": "2026-08-01"})
        assert period.days == services.DEFAULT_PERIOD_DAYS

    def test_an_enormous_explicit_range_is_capped(self) -> None:
        period = services.resolve_period({"start": "2000-01-01", "end": "2026-01-01"})
        assert period.days == services.MAX_PERIOD_DAYS
        assert period.end == date(2026, 1, 1)


class TestDailyActivity:
    def test_quiet_days_are_present_as_zeros(self, launched_campaign, make_message, organization) -> None:
        """Closing up empty days would imply a steadier send rate than happened."""
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.DELIVERED)

        activity = services.daily_activity(organization, services.ReportPeriod.last_days(7))

        assert len(activity) == 7
        assert [day.total for day in activity[:-1]] == [0] * 6
        assert activity[-1].total == 1

    def test_the_buckets_partition_the_messages(self, launched_campaign, make_message, organization) -> None:
        campaign = launched_campaign()
        for status in (
            MessageStatus.PENDING,
            MessageStatus.QUEUED,
            MessageStatus.SENDING,
            MessageStatus.SENT,
            MessageStatus.DELIVERED,
            MessageStatus.READ,
            MessageStatus.FAILED,
        ):
            make_message(campaign, status=status)

        today = services.daily_activity(organization, services.ReportPeriod.last_days(1))[0]

        # Seven messages, no double counting: the three in-flight statuses roll
        # up into one "pending" bucket and everything else stands alone.
        assert today.total == 7
        assert today.pending == 3
        assert (today.sent, today.delivered, today.read, today.failed) == (1, 1, 1, 1)
        assert today.reached == 3

    def test_messages_outside_the_period_are_excluded(self, launched_campaign, make_message, organization) -> None:
        campaign = launched_campaign()
        make_message(campaign, created_at=timezone.now() - timedelta(days=40))
        make_message(campaign)

        activity = services.daily_activity(organization, services.ReportPeriod.last_days(7))

        assert sum(day.total for day in activity) == 1


class TestOverview:
    def test_delivery_rate_is_measured_against_what_the_provider_accepted(
        self, launched_campaign, make_message, organization
    ) -> None:
        """
        A queued message has not failed to arrive — it has not been sent.

        Dividing by every message would let a large pending backlog drag the
        delivery rate towards zero and make a healthy campaign look broken.
        """
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.SENT)
        for _ in range(8):
            make_message(campaign, status=MessageStatus.QUEUED)

        overview = services.overview(organization, services.ReportPeriod.last_days(7))

        assert overview.messages == 10
        assert overview.reached == 2
        assert overview.delivery_rate == 50.0

    def test_read_counts_as_delivered(self, launched_campaign, make_message, organization) -> None:
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.READ)

        overview = services.overview(organization, services.ReportPeriod.last_days(7))

        assert overview.confirmed_delivered == 1
        assert overview.delivery_rate == 100.0
        assert overview.read_rate == 100.0

    def test_rates_are_zero_rather_than_undefined_when_nothing_was_sent(self, organization) -> None:
        overview = services.overview(organization, services.ReportPeriod.last_days(7))
        assert (overview.delivery_rate, overview.read_rate, overview.failure_rate) == (0.0, 0.0, 0.0)

    def test_failure_rate_is_measured_against_every_recipient(
        self, launched_campaign, make_message, organization
    ) -> None:
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.FAILED)
        for _ in range(3):
            make_message(campaign, status=MessageStatus.DELIVERED)

        assert services.overview(organization, services.ReportPeriod.last_days(7)).failure_rate == 25.0

    def test_consent_movement_is_reported(self, make_contact, organization) -> None:
        make_contact("Joined", opted_in=True)
        left = make_contact("Left", opted_in=True)
        left.opt_out()
        left.save()

        overview = services.overview(organization, services.ReportPeriod.last_days(7))

        assert overview.contacts_added == 2
        assert overview.opt_ins == 2
        assert overview.opt_outs == 1
        assert overview.net_consent_change == 1


class TestCampaignPerformance:
    def test_only_launched_campaigns_appear(self, make_campaign, launched_campaign, organization) -> None:
        """A draft has no performance to report, so it is not a row."""
        make_campaign("Still a draft")
        launched_campaign("Actually sent")

        rows = services.campaign_performance(organization, services.ReportPeriod.last_days(7))

        assert [row.name for row in rows] == ["Actually sent"]

    def test_a_campaign_launched_before_the_period_is_excluded(self, launched_campaign, organization) -> None:
        launched_campaign("Old news", days_ago=60)
        assert services.campaign_performance(organization, services.ReportPeriod.last_days(7)) == []

    def test_rates_come_from_the_shared_stats_object(
        self, launched_campaign, make_message, organization
    ) -> None:
        """The reports page and the monitoring page must not do their own maths."""
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.READ)
        make_message(campaign, status=MessageStatus.FAILED)
        make_message(campaign, status=MessageStatus.QUEUED)

        from messaging.services import campaign_stats

        row = services.campaign_performance(organization, services.ReportPeriod.last_days(7))[0]

        assert row.stats.as_dict() == campaign_stats(campaign).as_dict()
        assert row.stats.total == 4


class TestActiveCampaigns:
    def test_sending_and_paused_campaigns_are_listed(self, launched_campaign, organization) -> None:
        """A campaign an operator just paused must not vanish from their panel."""
        launched_campaign("Sending", status=CampaignStatus.PROCESSING)
        launched_campaign("Paused", status=CampaignStatus.PAUSED)
        launched_campaign("Finished", status=CampaignStatus.COMPLETED)

        names = {row.name for row in services.active_campaigns(organization)}

        assert names == {"Sending", "Paused"}

    def test_progress_is_reported_per_campaign(self, launched_campaign, make_message, organization) -> None:
        campaign = launched_campaign("Sending", status=CampaignStatus.PROCESSING)
        make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.QUEUED)

        row = services.active_campaigns(organization)[0]

        assert row.stats.total == 2
        assert row.stats.progress_percent == 50.0


class TestFailureReasons:
    def test_failures_are_grouped_by_the_provider_error(
        self, launched_campaign, make_message, organization
    ) -> None:
        campaign = launched_campaign()
        for _ in range(3):
            make_message(
                campaign,
                status=MessageStatus.FAILED,
                error_code="131026",
                error_message="Message undeliverable",
            )
        make_message(
            campaign, status=MessageStatus.FAILED, error_code="470", error_message="Outside window"
        )

        reasons = services.failure_reasons(organization, services.ReportPeriod.last_days(7))

        assert [(r.error_code, r.count) for r in reasons] == [("131026", 3), ("470", 1)]

    def test_the_number_of_affected_campaigns_is_reported(
        self, launched_campaign, make_message, organization
    ) -> None:
        """One bad number and a systemic problem look identical without this."""
        for name in ("First", "Second"):
            campaign = launched_campaign(name)
            make_message(
                campaign, status=MessageStatus.FAILED, error_code="131026", error_message="Bad"
            )

        reason = services.failure_reasons(organization, services.ReportPeriod.last_days(7))[0]

        assert reason.count == 2
        assert reason.affected_campaigns == 2

    def test_successful_messages_are_never_counted_as_failures(
        self, launched_campaign, make_message, organization
    ) -> None:
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.DELIVERED)

        assert services.failure_reasons(organization, services.ReportPeriod.last_days(7)) == []

    def test_recent_failures_span_campaigns(self, launched_campaign, make_message, organization) -> None:
        first = launched_campaign("First")
        second = launched_campaign("Second")
        make_message(first, status=MessageStatus.FAILED)
        make_message(second, status=MessageStatus.FAILED)

        assert len(services.recent_failures(organization)) == 2


class TestConsentSummary:
    def test_eligibility_matches_the_consent_rule(self, make_contact, organization) -> None:
        from contacts.models import ContactStatus

        make_contact("Consenting and active", opted_in=True)
        make_contact("Consenting but blocked", opted_in=True, status=ContactStatus.BLOCKED)
        make_contact("No consent")

        summary = services.consent_summary(organization)

        assert summary.total == 3
        assert summary.opted_in == 2
        # Consent alone is not enough: the contact must also be active.
        assert summary.eligible == 1
        assert summary.opted_out == 1

    def test_the_opt_in_source_is_reported(self, make_contact, organization) -> None:
        make_contact("Consenting", opted_in=True)

        sources = dict(services.consent_summary(organization).by_opt_in_source)

        assert sources == {"Entered manually by an operator": 1}

    def test_an_empty_list_reports_zero_rather_than_dividing_by_it(self, organization) -> None:
        assert services.consent_summary(organization).opt_in_rate == 0.0
