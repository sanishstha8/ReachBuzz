"""
REST API for campaigns.

Lifecycle actions are separate endpoints rather than a writable status field,
so every transition runs through the state machine in ``campaigns.services``.
Launching additionally requires the ``can_launch_campaigns`` capability, since
it is the one irreversible action in the system.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from campaigns import dispatch, services
from campaigns.filters import CampaignFilter
from campaigns.models import Campaign
from campaigns.serializers import (
    AudienceBreakdownSerializer,
    CampaignAudienceSerializer,
    CampaignCreateSerializer,
    CampaignLaunchSerializer,
    CampaignMessageSerializer,
    CampaignPreviewSerializer,
    CampaignSerializer,
    CampaignStatsSerializer,
)
from core.mixins import ListOnlyFilterMixin
from core.pagination import LargeResultsPagination
from core.permissions import CanLaunchCampaigns, CanManageCampaigns
from messaging.models import Message
from messaging.serializers import MessageSerializer
from messaging.services import campaign_stats

logger = logging.getLogger(__name__)


class CampaignViewSet(ListOnlyFilterMixin, viewsets.ModelViewSet):
    """CRUD plus the wizard steps and lifecycle actions."""

    serializer_class = CampaignSerializer
    permission_classes = [CanManageCampaigns]
    filterset_class = CampaignFilter
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at", "status", "total_recipients"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return (
            Campaign.objects.all()
            .select_related("template", "created_by")
            .prefetch_related("audience_entries__group")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return CampaignCreateSerializer
        return CampaignSerializer

    def perform_create(self, serializer) -> None:
        campaign = services.create_campaign(
            name=serializer.validated_data["name"],
            description=serializer.validated_data.get("description", ""),
            user=self.request.user,
            request=self.request,
        )
        serializer.instance = campaign

    def perform_update(self, serializer) -> None:
        from core.exceptions import InvalidStateTransition

        if not serializer.instance.is_editable:
            raise InvalidStateTransition(
                "A campaign can only be edited while it is a draft or scheduled."
            )
        serializer.save()

    def perform_destroy(self, instance) -> None:
        from core.exceptions import InvalidStateTransition

        if instance.status == "processing":
            raise InvalidStateTransition(
                "Cancel this campaign before deleting it; it is currently sending."
            )
        instance.delete()

    # -- Wizard steps -------------------------------------------------------

    @extend_schema(
        request=CampaignAudienceSerializer,
        responses={200: AudienceBreakdownSerializer},
        description="Set the campaign audience and return the eligibility breakdown.",
    )
    @action(detail=True, methods=["put", "patch"])
    def audience(self, request: Request, pk=None) -> Response:
        campaign = self.get_object()
        serializer = CampaignAudienceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        services.set_audience(
            campaign,
            serializer.validated_data.get("group_ids", []),
            target_all_eligible=serializer.validated_data["target_all_eligible"],
        )
        breakdown = services.audience_breakdown(campaign)
        return Response(
            AudienceBreakdownSerializer(
                {**breakdown.__dict__, "excluded_total": breakdown.excluded_total}
            ).data
        )

    @extend_schema(
        request=CampaignMessageSerializer,
        responses={200: CampaignSerializer},
        description="Set the template (or free-form text) and the variable mapping.",
    )
    @action(detail=True, methods=["put", "patch"])
    def message(self, request: Request, pk=None) -> Response:
        campaign = self.get_object()
        serializer = CampaignMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        campaign = services.set_message(
            campaign,
            message_type=data["message_type"],
            template=data.get("template"),
            body_text=data.get("body_text", ""),
            variable_mapping=data.get("variable_mapping") or {},
        )
        return Response(CampaignSerializer(campaign).data)

    @extend_schema(
        responses={200: CampaignPreviewSerializer},
        description="Recipient counts, a rendered sample, and anything blocking the send.",
    )
    @action(detail=True, methods=["get", "post"])
    def preview(self, request: Request, pk=None) -> Response:
        campaign = self.get_object()
        preview = services.preview_campaign(campaign)

        payload = {
            "campaign_id": campaign.pk,
            "campaign_name": campaign.name,
            "audience": {
                **preview.audience.__dict__,
                "excluded_total": preview.audience.excluded_total,
            },
            "recipient_count": preview.audience.eligible,
            "sample_recipient": preview.sample_recipient.name if preview.sample_recipient else "",
            "sample_text": preview.sample_text,
            "missing_variables": preview.missing_variables,
            "blockers": preview.blockers,
            "is_ready": preview.is_ready,
            "sending_available": dispatch.is_sending_available(),
        }
        return Response(CampaignPreviewSerializer(payload).data)

    # -- Lifecycle ----------------------------------------------------------

    @extend_schema(
        request=CampaignLaunchSerializer,
        responses={202: CampaignSerializer},
        description="Materialize recipients and hand the campaign to the sender.",
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanLaunchCampaigns],
        throttle_classes=[],
    )
    def launch(self, request: Request, pk=None) -> Response:
        campaign = self.get_object()
        serializer = CampaignLaunchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        campaign = services.launch_campaign(campaign, user=request.user, request=request)
        return Response(
            CampaignSerializer(campaign).data, status=http_status.HTTP_202_ACCEPTED
        )

    @extend_schema(responses={200: CampaignSerializer}, description="Pause a sending campaign.")
    @action(detail=True, methods=["post"], permission_classes=[CanLaunchCampaigns])
    def pause(self, request: Request, pk=None) -> Response:
        campaign = services.pause_campaign(self.get_object(), user=request.user, request=request)
        return Response(CampaignSerializer(campaign).data)

    @extend_schema(responses={200: CampaignSerializer}, description="Resume a paused campaign.")
    @action(detail=True, methods=["post"], permission_classes=[CanLaunchCampaigns])
    def resume(self, request: Request, pk=None) -> Response:
        campaign = services.resume_campaign(self.get_object(), user=request.user, request=request)
        return Response(CampaignSerializer(campaign).data)

    @extend_schema(
        responses={200: CampaignSerializer},
        description="Cancel a campaign. Messages already handed to the provider cannot be recalled.",
    )
    @action(detail=True, methods=["post"], permission_classes=[CanLaunchCampaigns])
    def cancel(self, request: Request, pk=None) -> Response:
        campaign = services.cancel_campaign(self.get_object(), user=request.user, request=request)
        return Response(CampaignSerializer(campaign).data)

    # -- Monitoring ---------------------------------------------------------

    @extend_schema(
        responses={200: CampaignStatsSerializer},
        description="Live status breakdown. The monitoring page polls this.",
    )
    @action(detail=True, methods=["get"])
    def stats(self, request: Request, pk=None) -> Response:
        campaign = self.get_object()
        payload = campaign_stats(campaign).as_dict()
        payload["status"] = campaign.status
        return Response(CampaignStatsSerializer(payload).data)

    @extend_schema(
        responses={200: MessageSerializer(many=True)},
        description="Recipient-level status. Filter with ?status=failed.",
    )
    @action(detail=True, methods=["get"], pagination_class=LargeResultsPagination)
    def messages(self, request: Request, pk=None) -> Response:
        campaign = self.get_object()
        queryset = (
            Message.objects.filter(campaign=campaign)
            .select_related("contact")
            .order_by("contact__name")
        )

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        page = self.paginate_queryset(queryset)
        serializer = MessageSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)
