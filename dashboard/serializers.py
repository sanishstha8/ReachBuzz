"""
Serializers for the reporting API.

Plain ``Serializer`` classes rather than model serializers: every payload here
is an aggregate, not a row, and describing the shape explicitly is what lets
drf-spectacular document it.
"""

from __future__ import annotations

from rest_framework import serializers


class ReportPeriodSerializer(serializers.Serializer):
    start = serializers.DateField()
    end = serializers.DateField()
    days = serializers.IntegerField()
    label = serializers.CharField()


class OverviewSerializer(serializers.Serializer):
    """Headline figures for a period."""

    period = ReportPeriodSerializer()
    messages = serializers.IntegerField()
    pending = serializers.IntegerField()
    sent = serializers.IntegerField()
    delivered = serializers.IntegerField()
    read = serializers.IntegerField()
    failed = serializers.IntegerField()
    reached = serializers.IntegerField()
    confirmed_delivered = serializers.IntegerField()
    campaigns_launched = serializers.IntegerField()
    recipients = serializers.IntegerField()
    contacts_added = serializers.IntegerField()
    opt_ins = serializers.IntegerField()
    opt_outs = serializers.IntegerField()
    net_consent_change = serializers.IntegerField()
    delivery_rate = serializers.FloatField()
    read_rate = serializers.FloatField()
    failure_rate = serializers.FloatField()


class DayActivitySerializer(serializers.Serializer):
    """One day of the activity series. The buckets are disjoint and sum to total."""

    day = serializers.DateField()
    pending = serializers.IntegerField()
    sent = serializers.IntegerField()
    delivered = serializers.IntegerField()
    read = serializers.IntegerField()
    failed = serializers.IntegerField()
    total = serializers.IntegerField()
    reached = serializers.IntegerField()


class ActivitySerializer(serializers.Serializer):
    period = ReportPeriodSerializer()
    days = DayActivitySerializer(many=True)


class CampaignPerformanceSerializer(serializers.Serializer):
    """A campaign and how its send turned out."""

    id = serializers.UUIDField(source="campaign.id")
    name = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField(source="status_label")
    template_name = serializers.CharField()
    started_at = serializers.DateTimeField(source="campaign.started_at", allow_null=True)
    completed_at = serializers.DateTimeField(source="campaign.completed_at", allow_null=True)
    total = serializers.IntegerField(source="stats.total")
    pending = serializers.IntegerField(source="stats.in_flight")
    sent = serializers.IntegerField(source="stats.sent")
    delivered = serializers.IntegerField(source="stats.delivered")
    read = serializers.IntegerField(source="stats.read")
    failed = serializers.IntegerField(source="stats.failed")
    progress_percent = serializers.FloatField(source="stats.progress_percent")
    delivery_rate = serializers.FloatField(source="stats.delivery_rate")
    failure_rate = serializers.FloatField(source="stats.failure_rate")


class FailureReasonSerializer(serializers.Serializer):
    error_code = serializers.CharField()
    error_message = serializers.CharField()
    count = serializers.IntegerField()
    affected_campaigns = serializers.IntegerField()


class ConsentSummarySerializer(serializers.Serializer):
    total = serializers.IntegerField()
    opted_in = serializers.IntegerField()
    eligible = serializers.IntegerField()
    opted_out = serializers.IntegerField()
    opt_in_rate = serializers.FloatField()
