"""
HTML views: the campaign wizard and monitoring pages.

The wizard is five addressable steps rather than a session-backed form. A draft
campaign is a real row from step 1 onward, so an operator can leave and come
back, and every step reads its state from the database rather than a cookie.
"""

from __future__ import annotations

import logging

from django.contrib import messages as django_messages
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import DeleteView, DetailView, FormView, ListView, View
from django.views.generic.edit import CreateView, UpdateView

from campaigns import dispatch, services
from campaigns.filters import CampaignFilter
from campaigns.forms import (
    CampaignAudienceForm,
    CampaignConfirmForm,
    CampaignDetailsForm,
    CampaignMessageForm,
)
from campaigns.models import Campaign, CampaignStatus
from core.exceptions import DomainError
from core.mixins import ActiveUserRequiredMixin, CapabilityRequiredMixin, PageTitleMixin
from messaging.models import Message, MessageStatus
from messaging.services import campaign_failure_reasons, campaign_stats

logger = logging.getLogger(__name__)

MANAGE_CAMPAIGNS = "can_manage_campaigns"
LAUNCH_CAMPAIGNS = "can_launch_campaigns"


def _can_send() -> bool:
    """
    Whether a launch would actually succeed right now.

    A registered dispatcher is not enough: the Celery sender registers at
    startup whether or not the broker is running, so this probes the queue too
    (the result is cached briefly).
    """
    from whatsapp.health import check_broker

    return dispatch.is_sending_available() and check_broker().reachable


def _sending_blocked_reason() -> str:
    from whatsapp.health import check_broker

    if not dispatch.is_sending_available():
        return "No message sender is registered."
    health = check_broker()
    return "" if health.reachable else health.detail


WIZARD_STEPS = [
    ("details", "Campaign name"),
    ("audience", "Audience"),
    ("message", "Message"),
    ("preview", "Preview"),
    ("confirm", "Confirm"),
]


class WizardContextMixin:
    """Supplies the step indicator shared by every wizard page."""

    wizard_step: str = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["wizard_steps"] = [
            {
                "key": key,
                "label": label,
                "number": index,
                "is_current": key == self.wizard_step,
                "is_done": index < self._current_index(),
            }
            for index, (key, label) in enumerate(WIZARD_STEPS, start=1)
        ]
        context["wizard_step"] = self.wizard_step
        return context

    def _current_index(self) -> int:
        for index, (key, _label) in enumerate(WIZARD_STEPS, start=1):
            if key == self.wizard_step:
                return index
        return 0


# ---------------------------------------------------------------------------
# List and detail
# ---------------------------------------------------------------------------


class CampaignListView(ActiveUserRequiredMixin, PageTitleMixin, ListView):
    model = Campaign
    template_name = "campaigns/campaign_list.html"
    context_object_name = "campaigns"
    paginate_by = 25
    page_title = "Campaigns"
    active_nav = "campaigns"

    def get_queryset(self):
        queryset = (
            Campaign.objects.all()
            .select_related("template", "created_by")
            .annotate(
                message_count=Count("messages", distinct=True),
                failed_count=Count(
                    "messages", filter=Q(messages__status=MessageStatus.FAILED), distinct=True
                ),
            )
            # Explicit ordering: aggregation drops Meta.ordering, and an
            # unordered queryset makes pagination non-deterministic — the same
            # row can appear on two pages.
            .order_by("-created_at")
        )
        self.filterset = CampaignFilter(self.request.GET, queryset=queryset)
        return self.filterset.qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["status_choices"] = CampaignStatus.choices
        context["totals"] = Campaign.objects.aggregate(
            total_count=Count("id"),
            draft_count=Count("id", filter=Q(status=CampaignStatus.DRAFT)),
            processing_count=Count("id", filter=Q(status=CampaignStatus.PROCESSING)),
            completed_count=Count("id", filter=Q(status=CampaignStatus.COMPLETED)),
        )
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        context["sending_available"] = _can_send()
        context["sending_blocked_reason"] = _sending_blocked_reason()
        return context


class CampaignDetailView(ActiveUserRequiredMixin, PageTitleMixin, DetailView):
    """Monitoring page: progress, breakdown and failed-message detail."""

    model = Campaign
    template_name = "campaigns/campaign_detail.html"
    context_object_name = "campaign"
    active_nav = "campaigns"

    def get_queryset(self):
        return Campaign.objects.select_related("template", "created_by").prefetch_related(
            "audience_entries__group"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        campaign = self.object
        context["page_title"] = campaign.name
        context["stats"] = campaign_stats(campaign)
        context["audience"] = services.audience_breakdown(campaign)
        context["sending_available"] = _can_send()

        base = Message.objects.filter(campaign=campaign).select_related("contact")
        context["failed_messages"] = base.filter(status=MessageStatus.FAILED)[:50]
        context["recent_messages"] = base.order_by("contact__name")[:25]
        context["message_total"] = base.count()

        # Grouped failures answer a different question from the list of failed
        # messages above it: "is this one bad number, or is something wrong
        # with the connection?" — which the per-row list cannot show at a glance.
        context["failure_reasons"] = campaign_failure_reasons(campaign)

        if campaign.is_editable:
            context["preview"] = services.preview_campaign(campaign)
        return context


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


class CampaignCreateView(CapabilityRequiredMixin, WizardContextMixin, PageTitleMixin, CreateView):
    """Step 1 — name it, creating the draft."""

    model = Campaign
    form_class = CampaignDetailsForm
    template_name = "campaigns/wizard_details.html"
    required_capability = MANAGE_CAMPAIGNS
    page_title = "New campaign"
    active_nav = "campaigns"
    wizard_step = "details"

    def form_valid(self, form) -> HttpResponse:
        campaign = services.create_campaign(
            name=form.cleaned_data["name"],
            description=form.cleaned_data.get("description", ""),
            user=self.request.user,
            request=self.request,
        )
        return redirect("campaigns:wizard-audience", pk=campaign.pk)


class CampaignDetailsUpdateView(
    CapabilityRequiredMixin, WizardContextMixin, PageTitleMixin, UpdateView
):
    """Step 1 again, for an existing draft."""

    model = Campaign
    form_class = CampaignDetailsForm
    template_name = "campaigns/wizard_details.html"
    required_capability = MANAGE_CAMPAIGNS
    active_nav = "campaigns"
    wizard_step = "details"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit {self.object.name}"
        context["campaign"] = self.object
        return context

    def form_valid(self, form) -> HttpResponse:
        if not self.object.is_editable:
            django_messages.error(self.request, "This campaign can no longer be edited.")
            return redirect("campaigns:detail", pk=self.object.pk)
        form.save()
        return redirect("campaigns:wizard-audience", pk=self.object.pk)


class CampaignAudienceView(CapabilityRequiredMixin, WizardContextMixin, PageTitleMixin, FormView):
    """Step 2 — pick the groups, and show what consent leaves you."""

    template_name = "campaigns/wizard_audience.html"
    form_class = CampaignAudienceForm
    required_capability = MANAGE_CAMPAIGNS
    active_nav = "campaigns"
    wizard_step = "audience"

    def dispatch(self, request, *args, **kwargs):
        self.campaign = get_object_or_404(Campaign, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self) -> dict:
        return {
            "target_all_eligible": self.campaign.target_all_eligible,
            "groups": self.campaign.audience_groups.all(),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["campaign"] = self.campaign
        context["page_title"] = f"{self.campaign.name} · Audience"
        context["breakdown"] = services.audience_breakdown(self.campaign)
        return context

    def form_valid(self, form) -> HttpResponse:
        try:
            services.set_audience(
                self.campaign,
                list(form.cleaned_data.get("groups") or []),
                target_all_eligible=form.cleaned_data.get("target_all_eligible", False),
            )
        except DomainError as exc:
            django_messages.error(self.request, exc.message)
            return redirect("campaigns:detail", pk=self.campaign.pk)

        return redirect("campaigns:wizard-message", pk=self.campaign.pk)


class CampaignMessageView(CapabilityRequiredMixin, WizardContextMixin, PageTitleMixin, FormView):
    """Step 3 — template and variable mapping."""

    template_name = "campaigns/wizard_message.html"
    form_class = CampaignMessageForm
    required_capability = MANAGE_CAMPAIGNS
    active_nav = "campaigns"
    wizard_step = "message"

    def dispatch(self, request, *args, **kwargs):
        self.campaign = get_object_or_404(Campaign, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def _selected_template(self):
        """The template whose variables the form should ask about."""
        from whatsapp.models import MessageTemplate

        template_id = self.request.POST.get("template") or self.request.GET.get("template")
        if template_id:
            return MessageTemplate.objects.filter(pk=template_id).first()
        return self.campaign.template

    def get_form_kwargs(self) -> dict:
        kwargs = super().get_form_kwargs()
        kwargs["template"] = self._selected_template()
        return kwargs

    def get_initial(self) -> dict:
        initial = {
            "message_type": self.campaign.message_type,
            "template": self.campaign.template_id,
            "body_text": self.campaign.body_text,
        }
        for token, spec in (self.campaign.variable_mapping or {}).items():
            if spec.get("source") == "contact_field":
                initial[f"var_source_{token}"] = spec.get("value", "")
            else:
                initial[f"var_value_{token}"] = spec.get("value", "")
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["campaign"] = self.campaign
        context["page_title"] = f"{self.campaign.name} · Message"
        context["selected_template"] = self._selected_template()
        return context

    def post(self, request, *args, **kwargs):
        """
        Choosing a template re-renders this step with that template's variable
        fields, rather than advancing. The form is rebuilt unbound so the newly
        appeared variable rows are not immediately flagged as missing.
        """
        if request.POST.get("reload_template"):
            template = self._selected_template()
            initial = self.get_initial()
            initial["message_type"] = request.POST.get("message_type") or initial["message_type"]
            initial["template"] = template.pk if template else None
            initial["body_text"] = request.POST.get("body_text", "")

            form = self.form_class(initial=initial, template=template)
            return self.render_to_response(self.get_context_data(form=form))

        return super().post(request, *args, **kwargs)

    def form_valid(self, form) -> HttpResponse:
        try:
            services.set_message(
                self.campaign,
                message_type=form.cleaned_data["message_type"],
                template=form.cleaned_data.get("template"),
                body_text=form.cleaned_data.get("body_text", ""),
                variable_mapping=form.variable_mapping(),
            )
        except DomainError as exc:
            for field, errors in (exc.details or {}).items():
                for error in errors:
                    form.add_error(None, f"{field}: {error}")
            if not exc.details:
                form.add_error(None, exc.message)
            return self.form_invalid(form)

        return redirect("campaigns:wizard-preview", pk=self.campaign.pk)


class CampaignPreviewView(CapabilityRequiredMixin, WizardContextMixin, PageTitleMixin, DetailView):
    """Step 4 — exactly what will be sent, and to how many people."""

    model = Campaign
    template_name = "campaigns/wizard_preview.html"
    context_object_name = "campaign"
    required_capability = MANAGE_CAMPAIGNS
    active_nav = "campaigns"
    wizard_step = "preview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"{self.object.name} · Preview"
        context["preview"] = services.preview_campaign(self.object)
        context["sending_available"] = _can_send()
        context["sending_blocked_reason"] = _sending_blocked_reason()
        return context


class CampaignConfirmView(CapabilityRequiredMixin, WizardContextMixin, PageTitleMixin, FormView):
    """Step 5 — the confirmation and the launch."""

    template_name = "campaigns/wizard_confirm.html"
    form_class = CampaignConfirmForm
    required_capability = LAUNCH_CAMPAIGNS
    permission_denied_message = "You do not have permission to send campaigns."
    active_nav = "campaigns"
    wizard_step = "confirm"

    def dispatch(self, request, *args, **kwargs):
        self.campaign = get_object_or_404(Campaign, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["campaign"] = self.campaign
        context["page_title"] = f"{self.campaign.name} · Confirm"
        context["preview"] = services.preview_campaign(self.campaign)
        context["sending_available"] = _can_send()
        context["sending_blocked_reason"] = _sending_blocked_reason()
        return context

    def form_valid(self, form) -> HttpResponse:
        try:
            campaign = services.launch_campaign(
                self.campaign, user=self.request.user, request=self.request
            )
        except DomainError as exc:
            django_messages.error(self.request, exc.message)
            for blocker in (exc.details or {}).get("blockers", []):
                django_messages.warning(self.request, blocker)
            return redirect("campaigns:wizard-preview", pk=self.campaign.pk)

        django_messages.success(
            self.request,
            f"{campaign.name} launched to {campaign.total_recipients} recipient(s).",
        )
        return redirect("campaigns:detail", pk=campaign.pk)


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------


class CampaignActionView(CapabilityRequiredMixin, View):
    """POST-only pause / resume / cancel."""

    required_capability = LAUNCH_CAMPAIGNS

    ACTIONS = {
        "pause": (services.pause_campaign, "paused"),
        "resume": (services.resume_campaign, "resumed"),
        "cancel": (services.cancel_campaign, "cancelled"),
    }

    def post(self, request: HttpRequest, pk, action: str) -> HttpResponse:
        campaign = get_object_or_404(Campaign, pk=pk)
        handler = self.ACTIONS.get(action)

        if handler is None:
            django_messages.error(request, "Unknown action.")
            return redirect("campaigns:detail", pk=campaign.pk)

        func, verb = handler
        try:
            func(campaign, user=request.user, request=request)
        except DomainError as exc:
            django_messages.error(request, exc.message)
        else:
            django_messages.success(request, f"{campaign.name} {verb}.")

        return redirect("campaigns:detail", pk=campaign.pk)


class CampaignDeleteView(CapabilityRequiredMixin, PageTitleMixin, DeleteView):
    model = Campaign
    template_name = "campaigns/campaign_confirm_delete.html"
    success_url = reverse_lazy("campaigns:list")
    required_capability = MANAGE_CAMPAIGNS
    page_title = "Delete campaign"
    active_nav = "campaigns"

    def form_valid(self, form) -> HttpResponse:
        campaign = self.get_object()
        if campaign.status == CampaignStatus.PROCESSING:
            django_messages.error(
                self.request, "Cancel this campaign before deleting it; it is currently sending."
            )
            return redirect("campaigns:detail", pk=campaign.pk)

        name = campaign.name
        response = super().form_valid(form)
        django_messages.success(self.request, f"Campaign {name} deleted.")
        return response


class CampaignMessagesView(ActiveUserRequiredMixin, PageTitleMixin, ListView):
    """Recipient-level status for one campaign."""

    template_name = "campaigns/campaign_messages.html"
    context_object_name = "messages_list"
    paginate_by = 50
    active_nav = "campaigns"

    def dispatch(self, request, *args, **kwargs):
        self.campaign = get_object_or_404(Campaign, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (
            Message.objects.filter(campaign=self.campaign)
            .select_related("contact")
            .order_by("contact__name")
        )
        status_filter = self.request.GET.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["campaign"] = self.campaign
        context["page_title"] = f"{self.campaign.name} · Recipients"
        context["stats"] = campaign_stats(self.campaign)
        context["status_choices"] = MessageStatus.choices
        context["current_status"] = self.request.GET.get("status", "")
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context
