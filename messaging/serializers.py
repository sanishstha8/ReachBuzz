"""Serializers for message records."""

from __future__ import annotations

from rest_framework import serializers

from messaging.models import Message, MessageStatusEvent


class MessageStatusEventSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)

    class Meta:
        model = MessageStatusEvent
        fields = (
            "id",
            "status",
            "status_display",
            "source",
            "source_display",
            "provider_timestamp",
            "error_code",
            "error_message",
            "created_at",
        )
        read_only_fields = fields


class MessageSerializer(serializers.ModelSerializer):
    """
    Recipient-level status.

    Everything is read-only: message state is owned by the sending worker and
    the provider's webhooks, never by an API client.
    """

    contact_name = serializers.CharField(source="contact.name", read_only=True)
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    preview_text = serializers.CharField(read_only=True)

    class Meta:
        model = Message
        fields = (
            "id",
            "campaign",
            "campaign_name",
            "contact",
            "contact_name",
            "to_phone_number",
            "message_type",
            "template_name",
            "template_language",
            "preview_text",
            "provider_message_id",
            "status",
            "status_display",
            "attempt_count",
            "error_code",
            "error_message",
            "queued_at",
            "sent_at",
            "delivered_at",
            "read_at",
            "failed_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class MessageDetailSerializer(MessageSerializer):
    """Adds the full status history and the rendered payload."""

    status_events = MessageStatusEventSerializer(many=True, read_only=True)

    class Meta(MessageSerializer.Meta):
        fields = MessageSerializer.Meta.fields + ("rendered_payload", "status_events")
        read_only_fields = fields


class GlobalMessageStatsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    sent = serializers.IntegerField()
    delivered = serializers.IntegerField()
    read = serializers.IntegerField()
    failed = serializers.IntegerField()
    pending = serializers.IntegerField()
