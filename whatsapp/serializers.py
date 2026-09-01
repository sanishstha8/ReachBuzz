"""Serializers for message templates."""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

from whatsapp.models import MessageTemplate, TemplateCategory, TemplateSource, TemplateStatus
from whatsapp.services.templates import preview_with_examples


class MessageTemplateSerializer(serializers.ModelSerializer):
    """
    Read representation.

    Approval state is read-only everywhere: this application mirrors Meta's
    decision, it does not make one.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    variable_count = serializers.IntegerField(read_only=True)
    is_local = serializers.BooleanField(read_only=True)
    is_usable = serializers.SerializerMethodField()
    unusable_reason = serializers.SerializerMethodField()
    preview_text = serializers.SerializerMethodField()

    class Meta:
        model = MessageTemplate
        fields = (
            "id",
            "name",
            "language",
            "category",
            "category_display",
            "source",
            "source_display",
            "status",
            "status_display",
            "header_text",
            "body_text",
            "footer_text",
            "variables",
            "variable_count",
            "example_values",
            "provider_template_id",
            "synced_at",
            "rejection_reason",
            "is_local",
            "is_usable",
            "unusable_reason",
            "preview_text",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_is_usable(self, obj) -> bool:
        return obj.usability().usable

    def get_unusable_reason(self, obj) -> str:
        return obj.usability().reason

    def get_preview_text(self, obj) -> str:
        return preview_with_examples(obj).full_text


class LocalTemplateCreateSerializer(serializers.ModelSerializer):
    """
    Create a **local** development template.

    Deliberately cannot set ``source``, ``status`` or ``provider_template_id``:
    nothing here may claim Meta approval. Local templates are refused at launch
    whenever the live provider is selected.
    """

    class Meta:
        model = MessageTemplate
        fields = ("id", "name", "language", "category", "header_text", "body_text", "footer_text",
                  "example_values")
        read_only_fields = ("id",)

    def validate_name(self, value: str) -> str:
        value = (value or "").strip().lower().replace(" ", "_")
        if not value:
            raise serializers.ValidationError("A template name is required.")
        return value

    def validate_body_text(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("The template body cannot be empty.")
        if len(value) > 1024:
            raise serializers.ValidationError(
                "WhatsApp template bodies are limited to 1024 characters."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        if getattr(settings, "WHATSAPP_PROVIDER", "mock") != "mock":
            raise serializers.ValidationError(
                "Local templates can only be created while the mock provider is active. "
                "With the live provider, create and submit templates in WhatsApp Manager "
                "and sync them here."
            )
        return attrs

    def create(self, validated_data: dict) -> MessageTemplate:
        validated_data["source"] = TemplateSource.LOCAL
        validated_data["status"] = TemplateStatus.NOT_SUBMITTED
        validated_data.setdefault("category", TemplateCategory.UTILITY)
        return super().create(validated_data)


class TemplateRenderRequestSerializer(serializers.Serializer):
    """Preview a template with caller-supplied values."""

    values = serializers.DictField(child=serializers.CharField(allow_blank=True), required=False)


class TemplateRenderSerializer(serializers.Serializer):
    header = serializers.CharField(allow_blank=True)
    body = serializers.CharField(allow_blank=True)
    footer = serializers.CharField(allow_blank=True)
    full_text = serializers.CharField(allow_blank=True)
    missing = serializers.ListField(child=serializers.CharField())
    is_complete = serializers.BooleanField()
