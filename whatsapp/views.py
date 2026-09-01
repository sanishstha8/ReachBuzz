"""HTML views for message templates."""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages as django_messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import DetailView, ListView
from django.views.generic.edit import CreateView

from campaigns.forms import LocalTemplateForm
from core.mixins import ActiveUserRequiredMixin, CapabilityRequiredMixin, PageTitleMixin
from whatsapp.models import MessageTemplate, TemplateSource, TemplateStatus
from whatsapp.services.templates import preview_with_examples

logger = logging.getLogger(__name__)


class TemplateListView(ActiveUserRequiredMixin, PageTitleMixin, ListView):
    model = MessageTemplate
    template_name = "whatsapp/template_list.html"
    context_object_name = "templates"
    paginate_by = 25
    page_title = "Message templates"
    active_nav = "templates"

    def get_queryset(self):
        queryset = MessageTemplate.objects.all()
        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset.order_by("name", "language")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        provider = getattr(settings, "WHATSAPP_PROVIDER", "mock")
        context["search"] = self.request.GET.get("search", "")
        context["provider"] = provider
        context["is_mock"] = provider == "mock"
        context["usable_count"] = MessageTemplate.objects.usable_with(provider).count()
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context


class TemplateDetailView(ActiveUserRequiredMixin, PageTitleMixin, DetailView):
    model = MessageTemplate
    template_name = "whatsapp/template_detail.html"
    context_object_name = "template"
    active_nav = "templates"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        template = self.object
        context["page_title"] = template.name
        context["rendered"] = preview_with_examples(template)
        context["usability"] = template.usability()
        context["campaign_count"] = template.campaigns.count()
        return context


class LocalTemplateCreateView(CapabilityRequiredMixin, PageTitleMixin, CreateView):
    """
    Create a development-only template.

    Administrators only, and only while the mock provider is active. Nothing
    here claims Meta approval: the record is stored as LOCAL / NOT_SUBMITTED,
    and campaign validation refuses it under the live provider.
    """

    model = MessageTemplate
    form_class = LocalTemplateForm
    template_name = "whatsapp/template_form.html"
    required_capability = "is_administrator"
    permission_denied_message = "Only administrators can create templates."
    page_title = "New local template"
    active_nav = "templates"

    def dispatch(self, request, *args, **kwargs):
        if getattr(settings, "WHATSAPP_PROVIDER", "mock") != "mock":
            django_messages.error(
                request,
                "Local templates can only be created while the mock provider is active. "
                "With the live provider, create and submit templates in WhatsApp Manager, "
                "then sync them here.",
            )
            return redirect("whatsapp:template-list")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form) -> HttpResponse:
        form.instance.source = TemplateSource.LOCAL
        form.instance.status = TemplateStatus.NOT_SUBMITTED
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        django_messages.success(
            self.request,
            f"Local template '{self.object.name}' created. It works with the mock provider only.",
        )
        return response

    def get_success_url(self) -> str:
        return reverse("whatsapp:template-detail", kwargs={"pk": self.object.pk})
