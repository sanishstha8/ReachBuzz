"""
CSV exports.

An export leaves the application and is opened somewhere else, usually in a
spreadsheet, so the tests here are mostly about what happens *after* the file
is downloaded: that a contact name cannot become a formula, that a credential
cannot appear, and that the file is a faithful copy of what the page showed.
"""

from __future__ import annotations

import csv
from datetime import timedelta

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from campaigns.models import CampaignStatus
from core.models import AuditAction, AuditLog
from dashboard import reports, services
from messaging.models import MessageStatus

pytestmark = pytest.mark.django_db


def _rows(response) -> list[list[str]]:
    body = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.reader(body.splitlines()))


def _download(client: Client, report: str, **params) -> list[list[str]]:
    url = reverse("dashboard:report-download", kwargs={"report": report})
    return _rows(client.get(url, params))


class TestFormulaInjection:
    """
    A contact name is untrusted text, and a spreadsheet executes some of it.

    Excel and Sheets evaluate a cell beginning ``=``, ``+``, ``-`` or ``@``, so
    an imported contact called ``=HYPERLINK(...)`` would run on the machine of
    whoever opens the export. Prefixing keeps it text.
    """

    @pytest.mark.parametrize("dangerous", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
    def test_a_cell_that_would_be_executed_is_defused(self, dangerous: str) -> None:
        assert reports._text(dangerous).startswith("'")

    def test_ordinary_text_is_left_alone(self) -> None:
        assert reports._text("Summer Sale") == "Summer Sale"

    def test_a_phone_number_stays_text(self) -> None:
        """E.164 numbers start with "+", which a spreadsheet would evaluate."""
        assert reports._text("+9779800000001") == "'+9779800000001"

    def test_a_missing_value_is_an_empty_cell_not_the_word_none(self) -> None:
        assert reports._text(None) == ""

    def test_a_hostile_contact_name_reaches_the_file_defused(
        self, auth_client, launched_campaign, make_message, make_contact
    ) -> None:
        campaign = launched_campaign("Summer")
        make_message(campaign, contact=make_contact("=cmd|'/c calc'!A1", opted_in=True))

        rows = _download(auth_client, "messages")

        assert rows[1][1].startswith("'=")


class TestCampaignReport:
    def test_the_header_is_written_even_with_no_rows(self, auth_client, organization) -> None:
        rows = _download(auth_client, "campaigns")
        assert rows == [list(reports.REPORTS["campaigns"].header)]

    def test_a_launched_campaign_is_a_row(
        self, auth_client, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign("Summer Sale")
        make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.FAILED)

        rows = _download(auth_client, "campaigns")

        assert len(rows) == 2
        assert rows[1][0] == "Summer Sale"
        assert rows[1][5] == "2"  # recipients

    def test_the_file_agrees_with_the_page(
        self, auth_client, launched_campaign, make_message, organization
    ) -> None:
        """A figure on screen and the same figure in the file cannot disagree."""
        campaign = launched_campaign("Summer Sale")
        for _ in range(3):
            make_message(campaign, status=MessageStatus.DELIVERED)
        make_message(campaign, status=MessageStatus.FAILED)

        row = _download(auth_client, "campaigns")[1]
        page = services.campaign_performance(organization, services.ReportPeriod.last_days(30))[0]

        assert row[11] == str(page.stats.delivery_rate)
        assert row[12] == str(page.stats.failure_rate)

    def test_a_draft_is_not_exported(self, auth_client, make_campaign) -> None:
        make_campaign("Never launched")
        assert len(_download(auth_client, "campaigns")) == 1


class TestMessageReport:
    def test_every_recipient_is_a_row(
        self, auth_client, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign()
        for _ in range(3):
            make_message(campaign, status=MessageStatus.SENT)

        assert len(_download(auth_client, "messages")) == 4

    def test_messages_outside_the_period_are_excluded(
        self, auth_client, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign()
        make_message(campaign, created_at=timezone.now() - timedelta(days=60))
        make_message(campaign)

        assert len(_download(auth_client, "messages", days=7)) == 2

    def test_the_error_the_provider_gave_is_carried_through(
        self, auth_client, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign()
        make_message(
            campaign,
            status=MessageStatus.FAILED,
            error_code="131026",
            error_message="Message undeliverable",
        )

        row = _download(auth_client, "messages")[1]

        assert row[6] == "131026"
        assert row[7] == "Message undeliverable"


class TestConsentReport:
    def test_every_contact_appears_whatever_their_consent(
        self, auth_client, make_contact
    ) -> None:
        make_contact("Consenting", opted_in=True)
        make_contact("Not consenting")

        rows = _download(auth_client, "consent")

        assert len(rows) == 3
        assert {row[4] for row in rows[1:]} == {"yes", "no"}

    def test_eligibility_is_reported_separately_from_consent(
        self, auth_client, make_contact
    ) -> None:
        """Consent alone does not make someone messageable; they must be active."""
        from contacts.models import ContactStatus

        make_contact("Blocked but consenting", opted_in=True, status=ContactStatus.BLOCKED)

        row = _download(auth_client, "consent")[1]

        assert row[4] == "yes"  # opted in
        assert row[5] == "no"  # can be messaged

    def test_the_period_is_ignored(self, auth_client, make_contact) -> None:
        """Consent is a state, so an old contact is still on today's register."""
        contact = make_contact("Joined long ago", opted_in=True)
        type(contact).objects.filter(pk=contact.pk).update(
            created_at=timezone.now() - timedelta(days=900)
        )

        assert len(_download(auth_client, "consent", days=7)) == 2

    def test_the_consent_source_is_recorded(self, auth_client, make_contact) -> None:
        make_contact("Consenting", opted_in=True)
        row = _download(auth_client, "consent")[1]
        assert row[6] == "Entered manually by an operator"


class TestFailureReport:
    def test_errors_are_grouped(self, auth_client, launched_campaign, make_message) -> None:
        campaign = launched_campaign()
        for _ in range(2):
            make_message(
                campaign, status=MessageStatus.FAILED, error_code="470", error_message="Window"
            )

        rows = _download(auth_client, "failures")

        assert rows[1] == ["470", "Window", "2", "1"]


class TestCampaignRecipientsReport:
    def test_only_that_campaign_is_exported(
        self, auth_client, launched_campaign, make_message
    ) -> None:
        wanted = launched_campaign("Wanted")
        other = launched_campaign("Other")
        make_message(wanted, status=MessageStatus.SENT)
        make_message(other, status=MessageStatus.SENT)

        response = auth_client.get(
            reverse("dashboard:campaign-recipients-report", kwargs={"pk": wanted.pk})
        )
        rows = _rows(response)

        assert len(rows) == 2
        assert rows[1][0] == "Wanted"

    def test_the_filename_is_derived_from_the_campaign(
        self, auth_client, launched_campaign, make_message
    ) -> None:
        campaign = launched_campaign("Q3 / promo #2")
        make_message(campaign)

        response = auth_client.get(
            reverse("dashboard:campaign-recipients-report", kwargs={"pk": campaign.pk})
        )

        # The prefix is slugify(SITE_NAME), so this follows a rename.
        prefix = reports.filename_prefix(settings.SITE_NAME)
        assert f'filename="{prefix}-campaign-q3-promo-2-recipients.csv"' in (
            response["Content-Disposition"]
        )

    def test_an_unknown_campaign_is_not_found(self, auth_client) -> None:
        import uuid

        response = auth_client.get(
            reverse("dashboard:campaign-recipients-report", kwargs={"pk": uuid.uuid4()})
        )
        assert response.status_code == 404


class TestResponse:
    def test_the_file_is_streamed(self, auth_client) -> None:
        """A large export must not be assembled in memory first."""
        response = auth_client.get(reverse("dashboard:report-download", kwargs={"report": "messages"}))
        assert response.streaming is True

    def test_it_is_offered_as_a_download_with_a_dated_name(self, auth_client) -> None:
        period = services.ReportPeriod.last_days(30)
        response = auth_client.get(
            reverse("dashboard:report-download", kwargs={"report": "campaigns"})
        )

        disposition = response["Content-Disposition"]
        assert disposition.startswith("attachment;")
        prefix = reports.filename_prefix(settings.SITE_NAME)
        assert f"{prefix}-campaigns-{period.slug}.csv" in disposition

    def test_it_is_never_cached(self, auth_client) -> None:
        """The file is a snapshot of live data; a cached copy goes stale at once."""
        response = auth_client.get(
            reverse("dashboard:report-download", kwargs={"report": "campaigns"})
        )
        assert response["Cache-Control"] == "no-store"

    def test_an_unknown_report_is_not_found(self, auth_client) -> None:
        response = auth_client.get(
            reverse("dashboard:report-download", kwargs={"report": "everything"})
        )
        assert response.status_code == 404

    def test_an_anonymous_visitor_cannot_download(self, client: Client) -> None:
        response = client.get(reverse("dashboard:report-download", kwargs={"report": "consent"}))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_no_credential_reaches_the_file(
        self, auth_client, settings, launched_campaign, make_message
    ) -> None:
        settings.META_ACCESS_TOKEN = "EAAtopsecrettoken1234567890"
        campaign = launched_campaign()
        make_message(campaign, status=MessageStatus.SENT)

        for slug in reports.REPORTS:
            body = b"".join(
                auth_client.get(
                    reverse("dashboard:report-download", kwargs={"report": slug})
                ).streaming_content
            ).decode()
            assert "EAAtopsecrettoken1234567890" not in body, slug


class TestAuditing:
    def test_an_export_is_recorded(self, auth_client, operator) -> None:
        """Personal data leaving the system belongs in the compliance trail."""
        auth_client.get(reverse("dashboard:report-download", kwargs={"report": "consent"}))

        entry = AuditLog.objects.get(action=AuditAction.REPORT_EXPORTED)
        assert entry.user == operator
        assert entry.metadata["report"] == "consent"

    def test_the_period_is_recorded_with_it(self, auth_client) -> None:
        auth_client.get(
            reverse("dashboard:report-download", kwargs={"report": "campaigns"}),
            {"start": "2026-08-01", "end": "2026-08-31"},
        )

        entry = AuditLog.objects.get(action=AuditAction.REPORT_EXPORTED)
        assert entry.metadata["period_start"] == "2026-08-01"
        assert entry.metadata["period_end"] == "2026-08-31"

    def test_a_state_report_records_no_misleading_period(self, auth_client) -> None:
        auth_client.get(reverse("dashboard:report-download", kwargs={"report": "consent"}))

        entry = AuditLog.objects.get(action=AuditAction.REPORT_EXPORTED)
        assert entry.metadata["period_start"] is None

    def test_a_campaign_export_is_recorded_against_the_campaign(
        self, auth_client, launched_campaign
    ) -> None:
        campaign = launched_campaign("Summer Sale", status=CampaignStatus.COMPLETED)

        auth_client.get(
            reverse("dashboard:campaign-recipients-report", kwargs={"pk": campaign.pk})
        )

        entry = AuditLog.objects.get(action=AuditAction.REPORT_EXPORTED)
        assert entry.object_type == "Campaign"
        assert entry.object_id == str(campaign.pk)

    def test_a_refused_download_is_not_recorded_as_an_export(self, auth_client) -> None:
        auth_client.get(reverse("dashboard:report-download", kwargs={"report": "everything"}))
        assert not AuditLog.objects.filter(action=AuditAction.REPORT_EXPORTED).exists()
