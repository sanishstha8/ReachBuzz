"""Serializers for contacts, groups and CSV imports."""

from __future__ import annotations

from rest_framework import serializers

from contacts.models import (
    Contact,
    ContactGroup,
    ContactImport,
    ContactImportRow,
    ContactStatus,
    GroupMembership,
)
from contacts.services import normalize_contact_phone
from core.exceptions import ValidationFailed


class ContactGroupSummarySerializer(serializers.ModelSerializer):
    """Compact group representation, embedded in contact payloads."""

    class Meta:
        model = ContactGroup
        fields = ("id", "name")
        read_only_fields = fields


class ContactSerializer(serializers.ModelSerializer):
    """
    Read/write representation of a contact.

    ``opted_in`` is read-only: consent is changed through the dedicated
    opt-in/opt-out actions so that every change records a source and an audit
    entry. Allowing it here would create a second, unaudited path.
    """

    groups = ContactGroupSummarySerializer(many=True, read_only=True)
    group_ids = serializers.PrimaryKeyRelatedField(
        queryset=ContactGroup.objects.all(),
        many=True,
        write_only=True,
        required=False,
        help_text="Groups to place this contact in.",
    )
    is_eligible = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Contact
        fields = (
            "id",
            "name",
            "phone_number",
            "country_code",
            "email",
            "status",
            "status_display",
            "opted_in",
            "opt_in_source",
            "opt_in_at",
            "opt_out_source",
            "opt_out_at",
            "is_eligible",
            "notes",
            "last_error_code",
            "last_error_message",
            "groups",
            "group_ids",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "country_code",
            "opted_in",
            "opt_in_source",
            "opt_in_at",
            "opt_out_source",
            "opt_out_at",
            "is_eligible",
            "last_error_code",
            "last_error_message",
            "created_at",
            "updated_at",
        )
        extra_kwargs = {
            # DRF would attach a UniqueValidator here because the column is
            # unique. That validator runs against the *raw* input, so
            # "+977 980 0000000" would slip past while "+9779800000000" would
            # not. Duplicate detection belongs after normalization, in
            # contacts.services, which also returns a 409 naming the existing
            # contact instead of a bare 400.
            "phone_number": {"validators": []},
        }

    def validate_phone_number(self, value: str) -> str:
        """Normalize to E.164 and surface a usable message when it fails."""
        try:
            e164, _ = normalize_contact_phone(value)
        except ValidationFailed as exc:
            raise serializers.ValidationError(exc.message) from exc
        return e164

    def validate_name(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value


class ContactCreateSerializer(ContactSerializer):
    """
    Creation accepts an explicit initial consent decision.

    ``opted_in`` defaults to False, so omitting it can never enrol someone into
    receiving messages by accident.
    """

    opted_in = serializers.BooleanField(
        default=False,
        help_text="Set only when consent has genuinely been obtained.",
    )

    class Meta(ContactSerializer.Meta):
        read_only_fields = tuple(
            field for field in ContactSerializer.Meta.read_only_fields if field != "opted_in"
        )


class ContactConsentSerializer(serializers.Serializer):
    """Payload for the opt-in / opt-out actions."""

    source = serializers.CharField(required=False, allow_blank=True, max_length=32)


class GroupMembershipSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True)
    phone_number = serializers.CharField(source="contact.phone_number", read_only=True)
    opted_in = serializers.BooleanField(source="contact.opted_in", read_only=True)

    class Meta:
        model = GroupMembership
        fields = ("id", "contact", "contact_name", "phone_number", "opted_in", "added_at")
        read_only_fields = fields


class ContactGroupSerializer(serializers.ModelSerializer):
    """
    Group with both a raw member count and an eligible count.

    Campaign planning needs the second number: the first tells you how big the
    list is, the second tells you how many of them you may actually message.
    """

    member_count = serializers.IntegerField(read_only=True)
    eligible_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ContactGroup
        fields = (
            "id",
            "name",
            "description",
            "member_count",
            "eligible_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "member_count", "eligible_count", "created_at", "updated_at")

    def validate_name(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value


class GroupMemberActionSerializer(serializers.Serializer):
    """Add or remove a set of contacts in one request."""

    contact_ids = serializers.PrimaryKeyRelatedField(
        queryset=Contact.objects.all(), many=True, allow_empty=False
    )


class ContactImportRowSerializer(serializers.ModelSerializer):
    outcome_display = serializers.CharField(source="get_outcome_display", read_only=True)

    class Meta:
        model = ContactImportRow
        fields = ("row_number", "outcome", "outcome_display", "raw_data", "error_message")
        read_only_fields = fields


class ContactImportSerializer(serializers.ModelSerializer):
    """The import report an operator sees after uploading a file."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    rows = ContactImportRowSerializer(many=True, read_only=True)
    target_group_name = serializers.CharField(source="target_group.name", read_only=True, default=None)

    class Meta:
        model = ContactImport
        fields = (
            "id",
            "file_name",
            "file_size",
            "status",
            "status_display",
            "total_rows",
            "imported_count",
            "updated_count",
            "duplicate_count",
            "invalid_count",
            "not_opted_in_count",
            "success_rate",
            "target_group",
            "target_group_name",
            "error_message",
            "started_at",
            "finished_at",
            "created_at",
            "rows",
        )
        read_only_fields = fields


class ContactImportCreateSerializer(serializers.Serializer):
    """Validates the upload request itself; the file is parsed by the importer."""

    file = serializers.FileField()
    update_existing = serializers.BooleanField(
        default=False,
        help_text="Update contacts that already hold a number in the file instead of skipping them.",
    )
    target_group = serializers.PrimaryKeyRelatedField(
        queryset=ContactGroup.objects.all(), required=False, allow_null=True
    )


class ContactStatsSerializer(serializers.Serializer):
    """Aggregate contact counts for the dashboard."""

    total = serializers.IntegerField()
    opted_in = serializers.IntegerField()
    opted_out = serializers.IntegerField()
    eligible = serializers.IntegerField()
    groups = serializers.IntegerField()
    by_status = serializers.DictField(child=serializers.IntegerField())


CONTACT_STATUS_CHOICES = ContactStatus.choices
