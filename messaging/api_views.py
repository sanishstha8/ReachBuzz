"""REST API for message records (read-only)."""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.mixins import ListOnlyFilterMixin
from core.pagination import LargeResultsPagination
from core.permissions import IsActiveUser
from messaging.models import Message, MessageStatus
from messaging.serializers import (
    GlobalMessageStatsSerializer,
    MessageDetailSerializer,
    MessageSerializer,
)
from messaging.services import global_stats
from organizations.scoping import OrganizationAwareMixin, OrganizationScopedViewSetMixin


class MessageFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=MessageStatus.choices)
    campaign = django_filters.UUIDFilter(field_name="campaign__id")
    contact = django_filters.UUIDFilter(field_name="contact__id")
    failed_only = django_filters.BooleanFilter(method="filter_failed_only")

    class Meta:
        model = Message
        fields = ["status", "campaign", "contact"]

    def filter_failed_only(self, queryset, name, value):
        return queryset.failed() if value else queryset


class MessageViewSet(OrganizationScopedViewSetMixin, ListOnlyFilterMixin, viewsets.ReadOnlyModelViewSet):
    """
    Message state is owned by the sending worker and the provider's webhooks,
    so this API is read-only by design.
    """

    permission_classes = [IsActiveUser]
    pagination_class = LargeResultsPagination
    filterset_class = MessageFilter
    search_fields = ["to_phone_number", "contact__name", "provider_message_id"]
    ordering_fields = ["created_at", "sent_at", "status"]
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = self.scoped(Message).select_related("campaign", "contact", "template")
        if self.action == "retrieve":
            return queryset.prefetch_related("status_events")
        return queryset

    def get_serializer_class(self):
        return MessageDetailSerializer if self.action == "retrieve" else MessageSerializer


class MessageStatsAPIView(OrganizationAwareMixin, APIView):
    """Aggregate message counts across every campaign."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: GlobalMessageStatsSerializer})
    def get(self, request: Request) -> Response:
        return Response(GlobalMessageStatsSerializer(global_stats(self.organization)).data)
