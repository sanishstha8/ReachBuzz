"""
Tenant isolation.

The rule is simple to state and easy to break: **a query for customer data must
always be filtered by organization.** Breaking it does not raise, does not fail
a smoke test, and does not look wrong in review — it just quietly returns
somebody else's contacts.

So the filtering is not left to call sites. Everything customer-owned inherits
:class:`OrganizationOwnedModel` and is fetched through
:meth:`OrganizationScopedQuerySet.for_organization`, and the view mixins below
resolve the organization from the request rather than from a URL. A view that
forgets is a view that gets nothing, not a view that leaks.

The dangerous shape this exists to prevent::

    Campaign.objects.get(pk=pk)                      # any customer's campaign
    Campaign.objects.for_organization(org).get(pk=pk)  # only this one's
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.http import Http404
from django.shortcuts import redirect

from organizations.models import Organization, OrganizationStatus


class OrganizationScopedQuerySet(models.QuerySet):
    def for_organization(self, organization) -> OrganizationScopedQuerySet:
        """
        Restrict to one organization.

        ``None`` returns nothing rather than everything. An unresolved
        organization is a bug, and the safe failure is an empty page — the
        unsafe one is every customer's data.
        """
        if organization is None:
            return self.none()
        return self.filter(organization=organization)

    def for_user(self, user) -> OrganizationScopedQuerySet:
        """Everything visible to a user across all their organizations."""
        if not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(organization__memberships__user=user).distinct()


class OrganizationOwnedModel(models.Model):
    """
    Base for every model a customer owns.

    Nullable for now: the column is added to tables that already hold rows, and
    those rows are given an organization by a data migration before the field
    is tightened. See ``organizations/migrations`` for the three-step retrofit.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="%(class)ss",
        db_index=True,
    )

    # Deliberately no `objects` here. Every model this is mixed into already
    # has its own manager (ContactQuerySet, CampaignQuerySet, ...), and a
    # manager declared on the abstract parent would be shadowed by the child's
    # anyway. Those querysets inherit OrganizationScopedQuerySet instead, so
    # they gain for_organization() without losing their own methods.

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Resolving the organization for a request
# ---------------------------------------------------------------------------


def organization_for(user) -> Organization | None:
    """
    The organization a user is acting for.

    One membership is the ordinary case today, so the first active one is the
    answer. When switching between organizations arrives, this is the single
    place that has to learn about the choice — which is why every caller goes
    through it instead of reaching for ``user.memberships.first()``.
    """
    if not getattr(user, "is_authenticated", False):
        return None

    membership = (
        user.memberships.select_related("organization")
        .filter(organization__status=OrganizationStatus.ACTIVE)
        .order_by("created_at")
        .first()
    )
    return membership.organization if membership else None


class OrganizationRequiredMixin:
    """
    Puts ``self.organization`` on the view and refuses if there is not one.

    A signed-in user with no organization cannot be shown customer data,
    because there is no answer to "whose?". Rather than silently rendering an
    empty page, they are sent somewhere that can explain.
    """

    #: Where to send a user who has no organization at all.
    no_organization_redirect = "dashboard:home"

    def dispatch(self, request, *args, **kwargs):
        self.organization = organization_for(request.user)

        if self.organization is None and request.user.is_authenticated:
            from django.contrib import messages

            messages.error(
                request,
                "Your account is not linked to an organization. Ask an administrator "
                "to add you to one.",
            )
            if request.resolver_match and request.resolver_match.view_name == (
                self.no_organization_redirect
            ):
                raise ImproperlyConfigured(
                    "no_organization_redirect points at this view; that would loop."
                )
            return redirect(self.no_organization_redirect)

        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        """Scope whatever the view was going to fetch."""
        queryset = super().get_queryset()
        if hasattr(queryset, "for_organization"):
            return queryset.for_organization(self.organization)
        return queryset.filter(organization=self.organization)


class OrganizationObjectMixin(OrganizationRequiredMixin):
    """
    For detail views. Turns another customer's id into a 404, not a record.

    404 rather than 403 on purpose: telling somebody that an object exists but
    is not theirs confirms it exists, which is itself a leak.
    """

    def get_object(self, queryset=None):
        queryset = queryset if queryset is not None else self.get_queryset()
        obj = queryset.filter(pk=self.kwargs.get(self.pk_url_kwarg or "pk")).first()
        if obj is None:
            raise Http404("No such object in this organization.")
        return obj


class OrganizationScopedViewSetMixin:
    """The DRF equivalent. Same rule, same failure mode."""

    def get_organization(self) -> Organization | None:
        if not hasattr(self, "_organization"):
            self._organization = organization_for(self.request.user)
        return self._organization

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.get_organization()
        if hasattr(queryset, "for_organization"):
            return queryset.for_organization(organization)
        if organization is None:
            return queryset.none()
        return queryset.filter(organization=organization)

    def perform_create(self, serializer):
        """Stamp new records with the caller's organization, never the payload's."""
        serializer.save(organization=self.get_organization())
