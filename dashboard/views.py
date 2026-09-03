"""
Dashboard, monitoring and reports.

Tiles show real numbers for the modules that have shipped and an em dash for
those that have not — a fabricated zero reads as "nothing has happened", which
is a claim the system cannot make about a module that does not exist yet.

The three pages here answer three different questions, which is why they are
three pages and not one: the dashboard answers *what is happening right now*,
the reports page answers *what happened over a period I choose*, and the CSV
downloads answer *give me the rows so I can do my own arithmetic*. All of them
read their figures from ``dashboard.services``, so they cannot disagree.
"""

from __future__ import annotations

import logging

from django.apps import apps
from django.conf import settings
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import TemplateView, View

from core.audit import record_audit
from core.mixins import ActiveUserRequiredMixin, PageTitleMixin
from core.models import AuditAction
from dashboard import charts, reports, services

logger = logging.getLogger(__name__)

# The dashboard chart is a glance, not an analysis: two weeks fits the card
# without crowding, and the reports page is one click away for anything longer.
DASHBOARD_CHART_DAYS = 14


class HomeView(ActiveUserRequiredMixin, PageTitleMixin, TemplateView):
    template_name = "dashboard/home.html"
    page_title = "Dashboard"
    active_nav = "dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["modules"] = {
            "contacts": apps.is_installed("contacts"),
            "campaigns": apps.is_installed("campaigns"),
            "messaging": apps.is_installed("messaging"),
            "whatsapp": apps.is_installed("whatsapp"),
        }
        context["contact_stats"] = self._contact_stats()
        context["campaign_stats"] = self._campaign_stats()
        context["message_stats"] = self._message_stats()
        context["recent_campaigns"] = self._recent_campaigns()
        context["system_status"] = self._system_status()

        period = services.ReportPeriod.last_days(DASHBOARD_CHART_DAYS)
        activity = services.daily_activity(self.organization, period)
        context["chart_period"] = period
        context["activity"] = activity
        context["chart"] = charts.stacked_column_chart(activity)
        context["chart_id"] = "dashboardActivity"

        active = services.active_campaigns(self.organization)
        context["active_campaigns"] = [
            {"row": row, "proportions": charts.stats_proportions(row.stats)} for row in active
        ]
        context["recent_failures"] = services.recent_failures(self.organization, limit=6)
        return context

    # -- Statistics ---------------------------------------------------------
    #
    # Each helper returns None while its app is not installed, which is what
    # makes the template render an em dash instead of a misleading zero.

    def _contact_stats(self) -> dict[str, int] | None:
        if not apps.is_installed("contacts"):
            return None

        from contacts.models import Contact, ContactGroup, ContactStatus

        aggregates = Contact.objects.for_organization(self.organization).aggregate(
            total_count=Count("id"),
            opted_in_count=Count("id", filter=Q(opted_in=True)),
            eligible_count=Count("id", filter=Q(opted_in=True, status=ContactStatus.ACTIVE)),
        )
        return {
            "total": aggregates["total_count"],
            "opted_in": aggregates["opted_in_count"],
            "eligible": aggregates["eligible_count"],
            "groups": ContactGroup.objects.for_organization(self.organization).count(),
        }

    def _campaign_stats(self) -> dict[str, int] | None:
        if not apps.is_installed("campaigns"):
            return None

        from campaigns.models import Campaign, CampaignStatus

        aggregates = Campaign.objects.for_organization(self.organization).aggregate(
            total_count=Count("id"),
            draft_count=Count("id", filter=Q(status=CampaignStatus.DRAFT)),
            processing_count=Count("id", filter=Q(status=CampaignStatus.PROCESSING)),
            completed_count=Count("id", filter=Q(status=CampaignStatus.COMPLETED)),
        )
        return {
            "total": aggregates["total_count"],
            "draft": aggregates["draft_count"],
            "processing": aggregates["processing_count"],
            "completed": aggregates["completed_count"],
        }

    def _message_stats(self) -> dict[str, int] | None:
        if not apps.is_installed("messaging"):
            return None

        from messaging.services import global_stats

        return global_stats(self.organization)

    def _recent_campaigns(self):
        if not apps.is_installed("campaigns"):
            return []

        from campaigns.models import Campaign
        from messaging.models import MessageStatus

        return (
            Campaign.objects.for_organization(self.organization)
            .select_related("template")
            .annotate(
                message_count=Count("messages", distinct=True),
                delivered_count=Count(
                    "messages",
                    filter=Q(messages__status__in=[MessageStatus.DELIVERED, MessageStatus.READ]),
                    distinct=True,
                ),
                failed_count=Count(
                    "messages", filter=Q(messages__status=MessageStatus.FAILED), distinct=True
                ),
            )
            .order_by("-created_at")[:8]
        )

    @staticmethod
    def _system_status() -> dict[str, object]:
        """
        Whether a campaign could actually be sent right now.

        "A dispatcher is registered" is not that answer — the Celery sender
        registers at startup whether or not Redis is running — so this probes
        the broker (cached briefly) rather than reporting a proxy for it.
        """
        status = {
            "provider": settings.WHATSAPP_PROVIDER,
            "celery_broker_configured": bool(settings.CELERY_BROKER_URL),
            "send_rate_per_second": settings.WHATSAPP_SEND_RATE_PER_SECOND,
            "default_country": settings.DEFAULT_COUNTRY_CODE,
            "dispatcher_registered": False,
            "broker_reachable": False,
            "broker_detail": "",
            "can_send": False,
        }

        if apps.is_installed("whatsapp"):
            from whatsapp.health import pipeline_status

            status.update(pipeline_status())

        return status


class ReportsView(ActiveUserRequiredMixin, PageTitleMixin, TemplateView):
    """
    Everything that happened in a period the reader chooses.

    One filter row scopes the whole page: the tiles, the chart, the campaign
    table and the failure table all describe the same slice, so two numbers on
    this page can never be measuring different windows. The consent panel is
    the deliberate exception, and says so — consent is a state, not an event.
    """

    template_name = "dashboard/reports.html"
    page_title = "Reports"
    active_nav = "reports"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        period = services.resolve_period(self.request.GET)
        activity = services.daily_activity(self.organization, period)

        context["period"] = period
        context["period_presets"] = services.PERIOD_PRESETS
        context["selected_preset"] = self._selected_preset(period)
        context["overview"] = services.overview(self.organization, period)
        context["activity"] = activity
        context["chart"] = charts.stacked_column_chart(activity)
        context["chart_id"] = "reportActivity"
        context["campaign_rows"] = [
            {"row": row, "proportions": charts.stats_proportions(row.stats)}
            for row in services.campaign_performance(self.organization, period)
        ]
        context["failure_reasons"] = services.failure_reasons(self.organization, period)
        context["consent"] = services.consent_summary(self.organization)
        context["reports"] = list(reports.REPORTS.values())
        context["time_zone"] = settings.TIME_ZONE
        # Explicit dates rather than ?days=: a download started from this page
        # must cover the period the reader was looking at, even if they come
        # back to the link tomorrow.
        context["period_query"] = f"start={period.start.isoformat()}&end={period.end.isoformat()}"
        return context

    @staticmethod
    def _selected_preset(period: services.ReportPeriod) -> int | None:
        """Which preset button to mark as current, if the period matches one."""
        if period.end != timezone.localdate():
            return None
        return next((days for days, _ in services.PERIOD_PRESETS if days == period.days), None)


class ReportDownloadView(ActiveUserRequiredMixin, View):
    """
    Stream one of the catalogued CSV reports.

    The export is audited before the response is returned. Rows are generated
    lazily, so the audit entry records that the file was requested — which is
    the fact the compliance trail needs — rather than waiting to find out
    whether the reader finished downloading it.
    """

    def get(self, request: HttpRequest, report: str) -> HttpResponse:
        spec = reports.REPORTS.get(report)
        if spec is None:
            raise Http404("No such report.")

        period = services.resolve_period(request.GET)
        filename = spec.filename(period, prefix=reports.filename_prefix(settings.SITE_NAME))

        record_audit(
            AuditAction.REPORT_EXPORTED,
            request=request,
            description=f"Exported the {spec.label} report",
            metadata={
                "report": spec.slug,
                "period_start": period.start.isoformat() if spec.uses_period else None,
                "period_end": period.end.isoformat() if spec.uses_period else None,
                "filename": filename,
            },
        )
        return reports.stream_csv(
            filename, spec.header, spec.build(self.organization, period)
        )


class CampaignRecipientsReportView(ActiveUserRequiredMixin, View):
    """The recipient-level export for one campaign, from its monitoring page."""

    def get(self, request: HttpRequest, pk) -> HttpResponse:
        from campaigns.models import Campaign

        campaign = get_object_or_404(Campaign, pk=pk)
        prefix = reports.filename_prefix(settings.SITE_NAME)
        # A campaign may legitimately be named "Q3 / promo #2"; the slug keeps
        # that out of the Content-Disposition header.
        name = slugify(campaign.name) or "campaign"
        filename = f"{prefix}-campaign-{name}-recipients.csv"

        record_audit(
            AuditAction.REPORT_EXPORTED,
            request=request,
            obj=campaign,
            description=f"Exported the recipients of {campaign.name}",
            metadata={"report": "campaign-recipients", "filename": filename},
        )
        return reports.stream_csv(
            filename,
            reports.CAMPAIGN_RECIPIENTS_HEADER,
            reports.campaign_message_rows(campaign),
        )
