"""View mixins shared by the HTML dashboard pages."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.shortcuts import redirect


class ActiveUserRequiredMixin(LoginRequiredMixin):
    """Login required, and the account must still be active."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_active:
            messages.error(request, "Your account has been deactivated.")
            return redirect("accounts:logout")
        return super().dispatch(request, *args, **kwargs)


class CapabilityRequiredMixin(ActiveUserRequiredMixin, AccessMixin):
    """
    Gate an HTML view behind a capability property on the user model, e.g.::

        class ContactCreateView(CapabilityRequiredMixin, CreateView):
            required_capability = "can_manage_contacts"
    """

    required_capability: str = ""
    permission_denied_message = "You do not have permission to view this page."

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.required_capability and not getattr(request.user, self.required_capability, False):
            messages.error(request, self.permission_denied_message)
            return redirect("dashboard:home")
        return super().dispatch(request, *args, **kwargs)


class ListOnlyFilterMixin:
    """
    Apply filter backends to the list route only.

    DRF's ``get_object()`` runs ``filter_queryset()`` before looking a record
    up. On a viewset with a FilterSet that means a query parameter intended for
    a nested sub-resource — ``/campaigns/{id}/messages/?status=failed``, where
    ``status`` also exists on the campaign filter — silently filters the
    *parent* away and returns a confusing 404 instead of the filtered list.
    """

    def filter_queryset(self, queryset):
        if getattr(self, "action", None) != "list":
            return queryset
        return super().filter_queryset(queryset)


class PageTitleMixin:
    """Puts ``page_title`` and ``active_nav`` into the template context."""

    page_title: str = ""
    active_nav: str = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("page_title", self.page_title)
        context.setdefault("active_nav", self.active_nav)
        return context
