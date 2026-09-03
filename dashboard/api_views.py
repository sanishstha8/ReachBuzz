"""
Read-only reporting API.

The same aggregates the HTML pages render, in JSON. Every endpoint takes the
same period parameters and answers from ``dashboard.services``, so a figure
fetched here and a figure on the page are the same figure — the API is not a
second implementation of the arithmetic.

There is nothing writable here by design: a report is derived from message and
consent state, and the only way to change it is to change that state through
the endpoints that own it.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsActiveUser
from dashboard import services
from dashboard.serializers import (
    ActivitySerializer,
    CampaignPerformanceSerializer,
    ConsentSummarySerializer,
    FailureReasonSerializer,
    OverviewSerializer,
)
from organizations.scoping import OrganizationAwareMixin

PERIOD_PARAMETERS = [
    OpenApiParameter(
        "days",
        int,
        description=(
            f"Length of the period, counting back from today. "
            f"Defaults to {services.DEFAULT_PERIOD_DAYS}, capped at {services.MAX_PERIOD_DAYS}. "
            "Ignored when start/end are given."
        ),
    ),
    OpenApiParameter("start", str, description="First day of the period (YYYY-MM-DD)."),
    OpenApiParameter("end", str, description="Last day of the period, inclusive (YYYY-MM-DD)."),
]


def _period_payload(period: services.ReportPeriod) -> dict:
    return {
        "start": period.start,
        "end": period.end,
        "days": period.days,
        "label": period.label,
    }


class ReportAPIView(OrganizationAwareMixin, APIView):
    """Shared period handling for every reporting endpoint."""

    permission_classes = [IsActiveUser]

    def get_period(self, request: Request) -> services.ReportPeriod:
        return services.resolve_period(request.query_params)


class ReportOverviewAPIView(ReportAPIView):
    """Headline messaging, campaign and consent figures for a period."""

    @extend_schema(parameters=PERIOD_PARAMETERS, responses={200: OverviewSerializer})
    def get(self, request: Request) -> Response:
        period = self.get_period(request)
        overview = services.overview(self.organization, period)
        payload = {
            "period": _period_payload(period),
            "messages": overview.messages,
            "pending": overview.pending,
            "sent": overview.sent,
            "delivered": overview.delivered,
            "read": overview.read,
            "failed": overview.failed,
            "reached": overview.reached,
            "confirmed_delivered": overview.confirmed_delivered,
            "campaigns_launched": overview.campaigns_launched,
            "recipients": overview.recipients,
            "contacts_added": overview.contacts_added,
            "opt_ins": overview.opt_ins,
            "opt_outs": overview.opt_outs,
            "net_consent_change": overview.net_consent_change,
            "delivery_rate": overview.delivery_rate,
            "read_rate": overview.read_rate,
            "failure_rate": overview.failure_rate,
        }
        return Response(OverviewSerializer(payload).data)


class ReportActivityAPIView(ReportAPIView):
    """
    Message outcomes per day.

    Quiet days are present with zeros: a caller plotting this must not close up
    the gaps, because the flat stretch is part of the answer.
    """

    @extend_schema(parameters=PERIOD_PARAMETERS, responses={200: ActivitySerializer})
    def get(self, request: Request) -> Response:
        period = self.get_period(request)
        payload = {
            "period": _period_payload(period),
            "days": services.daily_activity(self.organization, period),
        }
        return Response(ActivitySerializer(payload).data)


class ReportCampaignsAPIView(ReportAPIView):
    """Campaigns launched in the period, with their delivery outcomes."""

    @extend_schema(
        parameters=PERIOD_PARAMETERS, responses={200: CampaignPerformanceSerializer(many=True)}
    )
    def get(self, request: Request) -> Response:
        rows = services.campaign_performance(self.organization, self.get_period(request))
        return Response(CampaignPerformanceSerializer(rows, many=True).data)


class ReportFailuresAPIView(ReportAPIView):
    """Distinct provider errors in the period, most frequent first."""

    @extend_schema(parameters=PERIOD_PARAMETERS, responses={200: FailureReasonSerializer(many=True)})
    def get(self, request: Request) -> Response:
        reasons = services.failure_reasons(self.organization, self.get_period(request))
        return Response(FailureReasonSerializer(reasons, many=True).data)


class ConsentSummaryAPIView(OrganizationAwareMixin, APIView):
    """
    Current consent state.

    Takes no period: consent is a state, not an event, so "how many people may
    we message" is only answerable as of now.
    """

    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: ConsentSummarySerializer})
    def get(self, request: Request) -> Response:
        return Response(ConsentSummarySerializer(services.consent_summary(self.organization)).data)


class ActiveCampaignsAPIView(OrganizationAwareMixin, APIView):
    """
    Campaigns sending right now. The dashboard's live panel polls this.

    Paused campaigns are included, so a campaign an operator has just stopped
    does not vanish from the panel they are watching it in.
    """

    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: CampaignPerformanceSerializer(many=True)})
    def get(self, request: Request) -> Response:
        rows = services.active_campaigns(self.organization)
        return Response(CampaignPerformanceSerializer(rows, many=True).data)
