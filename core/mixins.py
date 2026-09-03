"""View mixins shared by the HTML dashboard pages."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.shortcuts import redirect


class ActiveUserRequiredMixin(LoginRequiredMixin):
    """
    Login required, the account must still be active, and the request knows
    which organization it is acting for.

    Resolving the organization here rather than in a separate mixin means every
    authenticated view has ``self.organization`` without having to remember to
    ask for it — and :meth:`scoped` is then the only way any of them reaches
    for customer data.

    A user with no organization is *not* redirected. ``scoped()`` returns
    nothing for them, so they see empty lists rather than another customer's
    data, and pages that are not about customer data — their own profile, for
    instance — keep working. Failing closed beats failing loudly here.
    """

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_active:
            messages.error(request, "Your account has been deactivated.")
            return redirect("accounts:logout")

        from organizations.scoping import organization_for

        self.organization = organization_for(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        """
        Scope the default queryset of any view that declares ``model = X``.

        Without this, a ``DetailView``/``UpdateView``/``DeleteView`` that never
        overrides ``get_queryset`` falls through to ``Model.objects.all()`` and
        happily serves — or deletes — another customer's record to anyone who
        knows its id. Thirteen views were in exactly that shape.

        Left untouched when the queryset is not organization-owned, and never
        called at all by views without one (a ``TemplateView``, say).
        """
        queryset = super().get_queryset()
        if hasattr(queryset, "for_organization"):
            return queryset.for_organization(self.organization)
        return queryset

    def scoped(self, model):
        """
        This request's rows of ``model``, and no others.

        Every view that builds a queryset over customer data starts here
        instead of at ``Model.objects``. Spelled at the call site on purpose:
        a security filter you can read in the method you are looking at beats
        one applied invisibly up an inheritance chain — and these views define
        ``get_queryset`` themselves, so a wrapping mixin could not intercept
        them in any case.
        """
        return model.objects.for_organization(self.organization)


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
