"""Query filters for campaigns."""

from __future__ import annotations

import django_filters
from django.db.models import QuerySet

from campaigns.models import Campaign, CampaignStatus


class CampaignFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search", label="Search")
    status = django_filters.ChoiceFilter(choices=CampaignStatus.choices)
    template = django_filters.UUIDFilter(field_name="template__id")
    created_after = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    created_before = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        model = Campaign
        fields = ["search", "status", "template"]

    def filter_search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.search(value)
