"""Query filters shared by the REST API and the HTML list views."""

from __future__ import annotations

import django_filters
from django.db.models import QuerySet

from contacts.models import Contact, ContactGroup, ContactStatus


class ContactFilter(django_filters.FilterSet):
    """
    Filters for the contact list.

    ``eligible`` is exposed as its own filter because "can I message this
    person?" is the question operators actually ask, and it is not answerable
    from ``opted_in`` alone.
    """

    search = django_filters.CharFilter(method="filter_search", label="Search")
    group = django_filters.ModelChoiceFilter(
        field_name="group_memberships__group",
        queryset=ContactGroup.objects.all(),
        label="Group",
    )
    opted_in = django_filters.BooleanFilter(field_name="opted_in")
    status = django_filters.ChoiceFilter(choices=ContactStatus.choices)
    eligible = django_filters.BooleanFilter(method="filter_eligible", label="Can be messaged")
    created_after = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    created_before = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        model = Contact
        fields = ["search", "group", "opted_in", "status", "eligible"]

    def filter_search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.search(value)

    def filter_eligible(self, queryset: QuerySet, name: str, value: bool) -> QuerySet:
        if value is None:
            return queryset
        if value:
            return queryset.eligible()
        return queryset.exclude(opted_in=True, status=ContactStatus.ACTIVE)


class ContactGroupFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(field_name="name", lookup_expr="icontains")

    class Meta:
        model = ContactGroup
        fields = ["search"]
