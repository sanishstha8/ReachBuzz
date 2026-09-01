"""Serializers for campaigns."""

from __future__ import annotations

from rest_framework import serializers

from campaigns.models import Campaign, CampaignMessageType, CampaignStatus
from contacts.models import ContactGroup
from contacts.serializers import ContactGroupSummarySerializer
from whatsapp.models import MessageTemplate
from whatsapp.serializers import MessageTemplateSerializer


class CampaignSerializer(serializers.ModelSerializer):
    """
    Read representation.

    ``status`` is read-only: lifecycle changes go through the launch / pause /
    resume / cancel actions, which enforce the state machine. A writable status
    field would let a client move a campaign anywhere.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    message_type_display = serializers.CharField(source="get_message_type_display", read_only=True)
    audience_groups = ContactGroupSummarySerializer(many=True, read_only=True)
    template_detail = MessageTemplateSerializer(source="template", read_only=True)
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True, default="")
    is_editable = serializers.BooleanField(read_only=True)
    can_launch = serializers.BooleanField(read_only=True)
    can_pause = serializers.BooleanField(read_only=True)
    can_resume = serializers.BooleanField(read_only=True)
    can_cancel = serializers.BooleanField(read_only=True)
    unmapped_variables = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = Campaign
        fields = (
            "id",
            "name",
            "description",
            "message_type",
            "message_type_display",
            "template",
            "template_detail",
            "body_text",
            "variable_mapping",
            "unmapped_variables",
            "audience_groups",
            "target_all_eligible",
            "status",
            "status_display",
            "scheduled_at",
            "started_at",
            "completed_at",
            "total_recipients",
            "failure_reason",
            "created_by",
            "created_by_name",
            "is_editable",
            "can_launch",
            "can_pause",
            "can_resume",
            "can_cancel",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "status",
            "started_at",
            "completed_at",
            "total_recipients",
            "failure_reason",
            "created_by",
            "created_at",
            "updated_at",
        )


class CampaignCreateSerializer(serializers.ModelSerializer):
    """Step 1 of the wizard: just enough to have a draft to work on."""

    class Meta:
        model = Campaign
        fields = ("id", "name", "description")
        read_only_fields = ("id",)

    def validate_name(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A campaign name is required.")
        return value


class CampaignAudienceSerializer(serializers.Serializer):
    """Step 2: who receives this."""

    group_ids = serializers.PrimaryKeyRelatedField(
        queryset=ContactGroup.objects.all(), many=True, required=False
    )
    target_all_eligible = serializers.BooleanField(default=False)

    def validate(self, attrs: dict) -> dict:
        if not attrs.get("target_all_eligible") and not attrs.get("group_ids"):
            raise serializers.ValidationError(
                {"group_ids": ["Select at least one group, or target all eligible contacts."]}
            )
        return attrs


class CampaignMessageSerializer(serializers.Serializer):
    """Step 3: what the message says."""

    message_type = serializers.ChoiceField(
        choices=CampaignMessageType.choices, default=CampaignMessageType.TEMPLATE
    )
    template = serializers.PrimaryKeyRelatedField(
        queryset=MessageTemplate.objects.all(), required=False, allow_null=True
    )
    body_text = serializers.CharField(required=False, allow_blank=True)
    variable_mapping = serializers.DictField(required=False, default=dict)


class AudienceBreakdownSerializer(serializers.Serializer):
    in_audience = serializers.IntegerField()
    eligible = serializers.IntegerField()
    excluded_not_opted_in = serializers.IntegerField()
    excluded_inactive = serializers.IntegerField()
    excluded_total = serializers.IntegerField()
    groups = serializers.ListField(child=serializers.CharField())
    targets_all = serializers.BooleanField()


class CampaignPreviewSerializer(serializers.Serializer):
    """Step 4: exactly what will happen if this is confirmed."""

    campaign_id = serializers.UUIDField()
    campaign_name = serializers.CharField()
    audience = AudienceBreakdownSerializer()
    recipient_count = serializers.IntegerField()
    sample_recipient = serializers.CharField(allow_blank=True)
    sample_text = serializers.CharField(allow_blank=True)
    missing_variables = serializers.ListField(child=serializers.CharField())
    blockers = serializers.ListField(child=serializers.CharField())
    is_ready = serializers.BooleanField()
    sending_available = serializers.BooleanField()


class CampaignLaunchSerializer(serializers.Serializer):
    """
    Step 5: the explicit confirmation.

    Requiring the checkbox in the payload, not just in the UI, means an API
    client cannot skip the acknowledgement that a real send is about to happen.
    """

    confirm = serializers.BooleanField()

    def validate_confirm(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError(
                "Confirm that you want to send this campaign to the listed recipients."
            )
        return value


class CampaignStatsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    pending = serializers.IntegerField()
    queued = serializers.IntegerField()
    sending = serializers.IntegerField()
    sent = serializers.IntegerField()
    delivered = serializers.IntegerField()
    read = serializers.IntegerField()
    failed = serializers.IntegerField()
    in_flight = serializers.IntegerField()
    processed = serializers.IntegerField()
    progress_percent = serializers.FloatField()
    delivery_rate = serializers.FloatField()
    failure_rate = serializers.FloatField()
    status = serializers.ChoiceField(choices=CampaignStatus.choices, required=False)
