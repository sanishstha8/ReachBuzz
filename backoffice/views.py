"""
The platform operator's view across all tenants.

Read-only, by omission rather than by promise: there is no form here, no POST
route and no action button. Changing a customer's data is Django admin's job,
which has its own audit trail and its own permission model, and duplicating that
here would mean two places to get authorization wrong instead of one.
"""

from __future__ import annotations

import logging

from django.shortcuts import get_object_or_404
from django.views.generic import ListView, TemplateView

from backoffice import services
from backoffice.access import RecordsTheLookMixin, StaffOnlyMixin
from core.mixins import PageTitleMixin
from organizations.models import Organization, OrganizationStatus

logger = logging.getLogger(__name__)


class OverviewView(StaffOnlyMixin, PageTitleMixin, TemplateView):
    """Platform-wide counts. Not audited: no customer is named on this page."""

    template_name = "backoffice/overview.html"
    page_title = "Platform overview"
    active_nav = "backoffice"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["overview"] = services.platform_overview()
        context["plans"] = services.plan_distribution()
        context["health"] = services.health()
        return context


class OrganizationListView(StaffOnlyMixin, PageTitleMixin, ListView):
    """
    Every customer, with the figures that decide who needs attention.

    Not audited per view either. This is a directory — the equivalent of a
    customer list on a wall — and auditing a page that names everybody would
    produce an entry that says nothing about who was actually looked at.
    """

    template_name = "backoffice/organizations.html"
    context_object_name = "organizations"
    paginate_by = 50
    page_title = "Organizations"
    active_nav = "backoffice"

    def get_queryset(self):
        return services.organizations_with_context(
            search=self.request.GET.get("q", "").strip(),
            status=self.request.GET.get("status", "").strip(),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search"] = self.request.GET.get("q", "")
        context["status"] = self.request.GET.get("status", "")
        context["statuses"] = OrganizationStatus.choices
        return context


class OrganizationDetailView(RecordsTheLookMixin, PageTitleMixin, TemplateView):
    """
    One customer's account. This is the page that gets audited.

    Opening it is a privacy event: it names a business, its owner, its members
    and what it has been doing. The entry is written before anything renders.
    """

    template_name = "backoffice/organization_detail.html"
    active_nav = "backoffice"

    def audit_target(self):
        """Memoized: the audit hook and the context both need it, once each."""
        if not hasattr(self, "_organization"):
            self._organization = get_object_or_404(
                Organization.objects.select_related("owner", "subscription__plan"),
                pk=self.kwargs["pk"],
            )
        return self._organization

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.audit_target()
        context.update(services.organization_detail(organization))
        context["page_title"] = organization.name
        return context


class HealthView(StaffOnlyMixin, PageTitleMixin, TemplateView):
    """What needs a human, ordered by how quietly it fails."""

    template_name = "backoffice/health.html"
    page_title = "Health"
    active_nav = "backoffice"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["health"] = services.health()
        return context
