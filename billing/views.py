"""
The customer-facing billing area.

Stages 3 to 5 built plans, usage, invoices and payments and gave a customer no
way to see any of it. This is that view — and it is the first place where the
answer to "what am I paying for?" is available to the person paying.

Two rules shape every page here.

**Nothing is invented.** An unpriced plan says "Pricing on request", a metric
with no ceiling shows no progress bar, and a period with no invoices says so
rather than showing an empty table styled like a real one. The same rule the
landing page has followed since Phase 8 — a fabricated zero reads as "nothing
has happened", which is a different claim from "this does not apply".

**Reading is not changing.** Any member can see the bill; only an owner or an
administrator can change the plan or cancel. That split already exists on
``OrganizationMember.can_administer`` and this reuses it rather than inventing a
second notion of who is in charge.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from billing import services, usage
from billing.invoicing import Invoice
from billing.models import Plan, SubscriptionStatus
from core.mixins import ActiveUserRequiredMixin, PageTitleMixin
from organizations.models import OrganizationMember

logger = logging.getLogger(__name__)


class BillingAccessMixin(ActiveUserRequiredMixin):
    """
    Adds ``can_manage_billing`` to the view and the template context.

    Everybody who can see the dashboard can see what it costs — hiding the bill
    from the people using the product helps nobody. Changing it is the part that
    needs a role.
    """

    active_nav = "billing"

    @property
    def can_manage_billing(self) -> bool:
        if not getattr(self, "organization", None):
            return False
        membership = OrganizationMember.objects.filter(
            organization=self.organization, user=self.request.user
        ).first()
        return bool(membership and membership.can_administer)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_manage_billing"] = self.can_manage_billing
        return context


class BillingRequiredMixin(BillingAccessMixin):
    """For the pages that change something. Refuses, rather than hiding."""

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        """
        Refuse before the view body runs.

        ``self.organization`` is normally set by ``ActiveUserRequiredMixin``,
        further down the chain — which is too late to stop a POST. So this
        resolves it itself and lets the mixin resolve it again; one small query
        on a billing mutation is cheaper than reordering a mixin every
        authenticated view in the project depends on.

        Anonymous users fall straight through to ``LoginRequiredMixin``.
        """
        if request.user.is_authenticated:
            from organizations.scoping import organization_for

            self.organization = organization_for(request.user)
            if not self.can_manage_billing:
                messages.error(
                    request, "Only an owner or administrator can change the subscription."
                )
                return redirect("billing:overview")
        return super().dispatch(request, *args, **kwargs)


class OverviewView(BillingAccessMixin, PageTitleMixin, TemplateView):
    """Plan, usage this period, and what happens next."""

    template_name = "billing/overview.html"
    page_title = "Billing"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        summary = usage.summary(self.organization)

        context["summary"] = summary
        context["plan"] = summary["plan"]
        context["subscription"] = summary["subscription"]
        # Ordered so the page reads the way somebody thinks about it: what they
        # send, then who they hold, then who can log in.
        context["metrics"] = [
            ("Messages this period", summary["metrics"]["max_messages_per_month"]),
            ("Contacts", summary["metrics"]["max_contacts"]),
            ("Team members", summary["metrics"]["max_team_members"]),
        ]
        context["recent_invoices"] = Invoice.objects.for_organization(self.organization)[:5]
        context["outstanding"] = Invoice.objects.for_organization(self.organization).outstanding()
        return context


class PlanListView(BillingAccessMixin, PageTitleMixin, ListView):
    """The catalogue, with the current plan marked."""

    template_name = "billing/plans.html"
    context_object_name = "plans"
    page_title = "Plans"

    def get_queryset(self):
        return Plan.objects.public()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        subscription = usage.subscription_for(self.organization)
        context["subscription"] = subscription
        context["current_plan_id"] = subscription.plan_id if subscription else None
        # What they are using now, so a downgrade that would not fit is visible
        # before they choose it rather than after.
        context["current_usage"] = usage.summary(self.organization)["metrics"]
        return context


class ChangePlanView(BillingRequiredMixin, View):
    """
    Move to another plan. POST only.

    A downgrade that would put the organization over a ceiling is refused with
    the numbers, not silently accepted. Accepting it would leave a customer
    immediately over a limit they did not know they were choosing, unable to add
    a contact and unsure why.
    """

    def post(self, request: HttpRequest, slug: str) -> HttpResponse:
        plan = Plan.objects.active().filter(slug=slug).first()
        if plan is None:
            raise Http404("No such plan.")

        subscription = usage.subscription_for(self.organization)
        if subscription is None:
            services.subscribe(self.organization, plan, user=request.user, request=request)
            messages.success(request, f"You are now on the {plan.name} plan.")
            return redirect("billing:overview")

        blockers = _downgrade_blockers(self.organization, plan)
        if blockers:
            for blocker in blockers:
                messages.error(request, blocker)
            return redirect("billing:plans")

        services.change_plan(subscription, plan, user=request.user, request=request)
        messages.success(request, f"Your plan is now {plan.name}.")
        return redirect("billing:overview")


def _downgrade_blockers(organization, plan: Plan) -> list[str]:
    """
    What already exceeds the plan being moved to.

    Checked against live counts rather than against the old plan's limits: the
    question is not "is this smaller?" but "does what they have fit?" — moving
    from unlimited to 10,000 contacts is fine for somebody holding 500.
    """
    labels = {
        "max_contacts": "contacts",
        "max_team_members": "team members",
        "max_messages_per_month": "messages sent this period",
    }
    blockers = []

    for metric, label in labels.items():
        ceiling = plan.limit(metric)
        if ceiling is None:
            continue
        used = usage.COUNTERS[metric](organization)
        if used > ceiling:
            blockers.append(
                f"The {plan.name} plan allows {ceiling:,} {label} and you have "
                f"{used:,}. Reduce them first, or choose a larger plan."
            )

    return blockers


class CancelView(BillingRequiredMixin, View):
    """
    Cancel at the end of the period already paid for.

    Not immediately: cutting somebody off the moment they click takes away time
    they have bought. The overview then offers to undo it, because a
    cancellation a month away is one people change their mind about.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        subscription = usage.subscription_for(self.organization)
        if subscription is None:
            messages.error(request, "There is no subscription to cancel.")
            return redirect("billing:overview")

        services.cancel(subscription, user=request.user, request=request)
        messages.success(
            request,
            "Your subscription will end on "
            f"{subscription.current_period_end:%d %B %Y}. You can undo this until then.",
        )
        return redirect("billing:overview")


class ResumeView(BillingRequiredMixin, View):
    def post(self, request: HttpRequest) -> HttpResponse:
        subscription = usage.subscription_for(self.organization)
        if subscription is None or not subscription.cancel_at_period_end:
            messages.info(request, "There is no pending cancellation.")
            return redirect("billing:overview")

        services.resume(subscription, user=request.user, request=request)
        messages.success(request, "Your subscription will continue.")
        return redirect("billing:overview")


class InvoiceListView(BillingAccessMixin, PageTitleMixin, ListView):
    template_name = "billing/invoices.html"
    context_object_name = "invoices"
    paginate_by = 25
    page_title = "Invoices"

    def get_queryset(self):
        return Invoice.objects.for_organization(self.organization).select_related("plan")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Said in words on the page rather than left as an empty table. "No
        # invoices because none are due" reads very differently from a bug.
        plan = usage.plan_for(self.organization)
        context["no_priced_plan"] = plan is None or plan.price is None
        return context


class InvoiceDetailView(BillingAccessMixin, PageTitleMixin, DetailView):
    template_name = "billing/invoice_detail.html"
    context_object_name = "invoice"

    def get_queryset(self):
        """
        Scoped, so another organization's invoice number is a 404.

        An invoice is the single most sensitive document this application
        renders — it carries a business name, an amount and a period — so this
        does not rely on the number being hard to guess.
        """
        return (
            Invoice.objects.for_organization(self.organization)
            .select_related("plan", "organization")
            .prefetch_related("lines", "payments")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Invoice {self.object.number}"
        context["subscription_statuses"] = SubscriptionStatus
        return context
