"""REST API for message templates."""

from __future__ import annotations

import logging

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from core.permissions import CanManageCampaigns, IsAdministrator
from whatsapp.models import MessageTemplate
from whatsapp.serializers import (
    LocalTemplateCreateSerializer,
    MessageTemplateSerializer,
    TemplateRenderRequestSerializer,
    TemplateRenderSerializer,
)
from whatsapp.services.templates import (
    preview_with_examples,
    render_template,
    sync_templates_from_provider,
)

logger = logging.getLogger(__name__)


class MessageTemplateViewSet(viewsets.ModelViewSet):
    """
    Templates are read-mostly.

    Update and delete are disabled entirely: a synced template belongs to Meta,
    and editing a local one after campaigns reference it would change what
    those campaigns claim to send. Create is limited to local development
    templates, administrators only.
    """

    permission_classes = [CanManageCampaigns]
    search_fields = ["name", "body_text"]
    ordering_fields = ["name", "status", "updated_at"]
    ordering = ["name"]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = MessageTemplate.objects.all()

        usable_only = self.request.query_params.get("usable")
        if usable_only in ("true", "1"):
            queryset = queryset.usable_with(getattr(settings, "WHATSAPP_PROVIDER", "mock"))
        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return LocalTemplateCreateSerializer
        return MessageTemplateSerializer

    def get_permissions(self):
        if self.action in ("create", "sync"):
            return [IsAdministrator()]
        return super().get_permissions()

    def create(self, request: Request, *args, **kwargs) -> Response:
        """
        Create a local template and return its **canonical** representation.

        The create serializer is deliberately narrow (it cannot set approval
        state), but echoing that back would omit the derived fields — variables,
        usability — that a client needs immediately after creating.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = serializer.save(created_by=request.user)

        return Response(
            MessageTemplateSerializer(template).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        request=TemplateRenderRequestSerializer,
        responses={200: TemplateRenderSerializer},
        description="Render this template with the supplied values, for a safe preview.",
    )
    @action(detail=True, methods=["post"])
    def render(self, request: Request, pk=None) -> Response:
        template = self.get_object()
        serializer = TemplateRenderRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        values = serializer.validated_data.get("values") or {}
        rendered = render_template(template, values) if values else preview_with_examples(template)

        return Response(
            TemplateRenderSerializer(
                {
                    "header": rendered.header,
                    "body": rendered.body,
                    "footer": rendered.footer,
                    "full_text": rendered.full_text,
                    "missing": rendered.missing,
                    "is_complete": rendered.is_complete,
                }
            ).data
        )

    @extend_schema(
        request=None,
        responses={200: MessageTemplateSerializer(many=True)},
        description=(
            "Pull approved templates from the configured provider. Implemented "
            "alongside the provider integration."
        ),
    )
    @action(detail=False, methods=["post"])
    def sync(self, request: Request) -> Response:
        count = sync_templates_from_provider(user=request.user)
        templates = MessageTemplate.objects.all()
        return Response(
            {"synced": count, "results": MessageTemplateSerializer(templates, many=True).data}
        )
