"""
REST API for contacts, groups and CSV import.

Views stay thin: they validate input, delegate to ``contacts.services`` or
``contacts.importers``, and serialize the result. Every write path is guarded
by a capability permission, so a Viewer can read the audience but never change
who is on it.
"""

from __future__ import annotations

import logging

from django.db.models import Count, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from contacts import services
from contacts.filters import ContactFilter, ContactGroupFilter
from contacts.importers import import_contacts_from_file
from contacts.models import (
    Contact,
    ContactGroup,
    ContactImport,
    ContactStatus,
    OptInSource,
    OptOutSource,
)
from contacts.serializers import (
    ContactConsentSerializer,
    ContactCreateSerializer,
    ContactGroupSerializer,
    ContactImportCreateSerializer,
    ContactImportSerializer,
    ContactSerializer,
    ContactStatsSerializer,
    GroupMemberActionSerializer,
    GroupMembershipSerializer,
)
from core.mixins import ListOnlyFilterMixin
from core.pagination import LargeResultsPagination
from core.permissions import CanManageContacts, IsActiveUser

logger = logging.getLogger(__name__)


class ContactViewSet(ListOnlyFilterMixin, viewsets.ModelViewSet):
    """CRUD for contacts, plus the audited consent actions."""

    serializer_class = ContactSerializer
    permission_classes = [CanManageContacts]
    filterset_class = ContactFilter
    search_fields = ["name", "phone_number", "email"]
    ordering_fields = ["name", "created_at", "updated_at", "opted_in"]
    ordering = ["name"]

    def get_queryset(self):
        # Both relations, because they are different paths to the same rows:
        # the serializer reads the `groups` many-to-many, while `group_names`
        # walks the `group_memberships` through model. Prefetching only the
        # through relation — as this did — leaves the serializer issuing one
        # query per contact, which is invisible locally and a page-load per
        # row in production.
        return Contact.objects.all().prefetch_related("groups", "group_memberships__group")

    def get_serializer_class(self):
        if self.action == "create":
            return ContactCreateSerializer
        return ContactSerializer

    def perform_create(self, serializer) -> None:
        data = serializer.validated_data
        contact = services.create_contact(
            name=data["name"],
            phone_number=data["phone_number"],
            email=data.get("email", ""),
            status=data.get("status", ContactStatus.ACTIVE),
            opted_in=data.get("opted_in", False),
            opt_in_source=OptInSource.MANUAL if data.get("opted_in") else "",
            notes=data.get("notes", ""),
            groups=list(data.get("group_ids", [])),
            user=self.request.user,
            request=self.request,
        )
        serializer.instance = contact

    def perform_update(self, serializer) -> None:
        data = dict(serializer.validated_data)
        groups = data.pop("group_ids", None)

        contact = services.update_contact(
            serializer.instance,
            user=self.request.user,
            request=self.request,
            **data,
        )
        if groups is not None:
            services.set_contact_groups(contact, list(groups), user=self.request.user)
        serializer.instance = contact

    def perform_destroy(self, instance) -> None:
        services.delete_contact(instance, user=self.request.user, request=self.request)

    @extend_schema(
        request=ContactConsentSerializer,
        responses={200: ContactSerializer},
        description="Record consent for this contact. Audited.",
    )
    @action(detail=True, methods=["post"], url_path="opt-in")
    def opt_in(self, request: Request, pk=None) -> Response:
        contact = self.get_object()
        serializer = ContactConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        contact = services.set_consent(
            contact,
            opted_in=True,
            source=serializer.validated_data.get("source") or OptInSource.MANUAL,
            user=request.user,
            request=request,
        )
        return Response(ContactSerializer(contact).data)

    @extend_schema(
        request=ContactConsentSerializer,
        responses={200: ContactSerializer},
        description="Withdraw consent for this contact. Audited.",
    )
    @action(detail=True, methods=["post"], url_path="opt-out")
    def opt_out(self, request: Request, pk=None) -> Response:
        contact = self.get_object()
        serializer = ContactConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        contact = services.set_consent(
            contact,
            opted_in=False,
            source=serializer.validated_data.get("source") or OptOutSource.MANUAL,
            user=request.user,
            request=request,
        )
        return Response(ContactSerializer(contact).data)

    @extend_schema(
        responses={200: None},
        description=(
            "Message history for this contact. Returns an empty list until the "
            "messaging app ships in Phase 4."
        ),
    )
    @action(detail=True, methods=["get"], url_path="messages")
    def messages(self, request: Request, pk=None) -> Response:
        contact = self.get_object()
        history = getattr(contact, "messages", None)
        if history is None:
            return Response({"count": 0, "results": [], "detail": "Message history is not available yet."})
        return Response({"count": history.count(), "results": []})


class ContactGroupViewSet(ListOnlyFilterMixin, viewsets.ModelViewSet):
    """CRUD for groups, plus membership management."""

    serializer_class = ContactGroupSerializer
    permission_classes = [CanManageContacts]
    filterset_class = ContactGroupFilter
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]

    def get_queryset(self):
        return ContactGroup.objects.with_counts()

    def perform_create(self, serializer) -> None:
        serializer.save(created_by=self.request.user)

    @extend_schema(
        responses={200: GroupMembershipSerializer(many=True)},
        parameters=[OpenApiParameter("page", int, description="Page number.")],
        description="List the contacts in this group.",
    )
    @action(detail=True, methods=["get"], pagination_class=LargeResultsPagination)
    def members(self, request: Request, pk=None) -> Response:
        group = self.get_object()
        queryset = group.memberships.select_related("contact").order_by("contact__name")

        page = self.paginate_queryset(queryset)
        serializer = GroupMembershipSerializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @extend_schema(
        request=GroupMemberActionSerializer,
        responses={200: ContactGroupSerializer},
        description="Add contacts to this group. Adding an existing member is a no-op.",
    )
    @action(detail=True, methods=["post"], url_path="add-members")
    def add_members(self, request: Request, pk=None) -> Response:
        group = self.get_object()
        serializer = GroupMemberActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        services.add_contacts_to_group(
            group, list(serializer.validated_data["contact_ids"]), user=request.user, request=request
        )
        group = self.get_queryset().get(pk=group.pk)
        return Response(ContactGroupSerializer(group).data)

    @extend_schema(
        request=GroupMemberActionSerializer,
        responses={200: ContactGroupSerializer},
        description="Remove contacts from this group.",
    )
    @action(detail=True, methods=["post"], url_path="remove-members")
    def remove_members(self, request: Request, pk=None) -> Response:
        group = self.get_object()
        serializer = GroupMemberActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        services.remove_contacts_from_group(group, list(serializer.validated_data["contact_ids"]))
        group = self.get_queryset().get(pk=group.pk)
        return Response(ContactGroupSerializer(group).data)


class ContactImportViewSet(viewsets.ReadOnlyModelViewSet):
    """Import history and per-row reports."""

    serializer_class = ContactImportSerializer
    permission_classes = [CanManageContacts]
    ordering = ["-created_at"]

    def get_queryset(self):
        return ContactImport.objects.select_related("target_group").prefetch_related("rows")


class ContactImportCreateAPIView(APIView):
    """
    Upload a CSV file.

    The file is validated and imported synchronously; for the file sizes this
    application accepts that is well within a request. The importer itself is a
    pure service, so moving it onto Celery in Phase 5 changes only this view.
    """

    permission_classes = [CanManageContacts]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "csv_import"
    serializer_class = ContactImportCreateSerializer

    @extend_schema(
        request=ContactImportCreateSerializer,
        responses={201: ContactImportSerializer},
        description="Import contacts from a CSV file and return the import report.",
    )
    def post(self, request: Request) -> Response:
        serializer = ContactImportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        contact_import = import_contacts_from_file(
            serializer.validated_data["file"],
            update_existing=serializer.validated_data.get("update_existing", False),
            target_group=serializer.validated_data.get("target_group"),
            user=request.user,
            request=request,
        )
        return Response(
            ContactImportSerializer(contact_import).data, status=status.HTTP_201_CREATED
        )


class ContactStatsAPIView(APIView):
    """Aggregate contact counts, used by the dashboard tiles."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: ContactStatsSerializer})
    def get(self, request: Request) -> Response:
        # Aliases must not reuse a field name: Django would resolve the filter
        # reference to the alias and refuse to aggregate over an aggregate.
        aggregates = Contact.objects.aggregate(
            total_count=Count("id"),
            opted_in_count=Count("id", filter=Q(opted_in=True)),
            eligible_count=Count("id", filter=Q(opted_in=True, status=ContactStatus.ACTIVE)),
        )
        by_status = {
            row["status"]: row["count"]
            for row in Contact.objects.values("status").annotate(count=Count("id"))
        }

        data = {
            "total": aggregates["total_count"],
            "opted_in": aggregates["opted_in_count"],
            "opted_out": aggregates["total_count"] - aggregates["opted_in_count"],
            "eligible": aggregates["eligible_count"],
            "groups": ContactGroup.objects.count(),
            "by_status": {choice: by_status.get(choice, 0) for choice, _ in ContactStatus.choices},
        }
        return Response(ContactStatsSerializer(data).data)
