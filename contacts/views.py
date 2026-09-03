"""
HTML views for contact management.

These render the operator-facing pages. All mutations go through
``contacts.services`` / ``contacts.importers`` — the same code paths the REST
API uses — so business rules and auditing cannot diverge between the two.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import DeleteView, DetailView, FormView, ListView, TemplateView, View
from django.views.generic.edit import CreateView, UpdateView

from contacts import services
from contacts.filters import ContactFilter
from contacts.forms import ContactForm, ContactGroupForm, ContactImportForm
from contacts.importers import CsvImportError, import_contacts_from_file
from contacts.models import (
    Contact,
    ContactGroup,
    ContactImport,
    ContactStatus,
    OptInSource,
    OptOutSource,
    RowOutcome,
)
from core.mixins import ActiveUserRequiredMixin, CapabilityRequiredMixin, PageTitleMixin

logger = logging.getLogger(__name__)

MANAGE_CONTACTS = "can_manage_contacts"


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


class ContactListView(ActiveUserRequiredMixin, PageTitleMixin, ListView):
    """Searchable, filterable, paginated contact table."""

    model = Contact
    template_name = "contacts/contact_list.html"
    context_object_name = "contacts"
    paginate_by = 25
    page_title = "Contacts"
    active_nav = "contacts"

    def get_queryset(self):
        queryset = (
            self.scoped(Contact)
            .prefetch_related("group_memberships__group")
            .order_by(self.request.GET.get("sort") or "name")
        )
        self.filterset = ContactFilter(self.request.GET, queryset=queryset)
        return self.filterset.qs.distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filterset"] = self.filterset
        context["groups"] = ContactGroup.objects.order_by("name")
        context["status_choices"] = ContactStatus.choices
        # Aliases must not reuse a field name, or Django resolves the filter
        # reference to the alias and refuses to aggregate over an aggregate.
        totals = Contact.objects.aggregate(
            total_count=Count("id"),
            opted_in_count=Count("id", filter=Q(opted_in=True)),
            eligible_count=Count("id", filter=Q(opted_in=True, status=ContactStatus.ACTIVE)),
        )
        context["totals"] = {
            "total": totals["total_count"],
            "opted_in": totals["opted_in_count"],
            "eligible": totals["eligible_count"],
        }
        # Preserve filters across pagination links.
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context


class ContactDetailView(ActiveUserRequiredMixin, PageTitleMixin, DetailView):
    """Single contact, its groups, consent history and message history."""

    model = Contact
    template_name = "contacts/contact_detail.html"
    context_object_name = "contact"
    active_nav = "contacts"

    def get_queryset(self):
        return self.scoped(Contact).prefetch_related("group_memberships__group")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contact = self.object
        context["page_title"] = contact.name
        context["memberships"] = contact.group_memberships.select_related("group")
        context["available_groups"] = ContactGroup.objects.exclude(
            memberships__contact=contact
        ).order_by("name")
        # Populated once the messaging app ships in Phase 4.
        context["message_history"] = []
        return context


class ContactCreateView(CapabilityRequiredMixin, PageTitleMixin, CreateView):
    model = Contact
    form_class = ContactForm
    template_name = "contacts/contact_form.html"
    required_capability = MANAGE_CONTACTS
    page_title = "Add contact"
    active_nav = "contacts"

    def form_valid(self, form) -> HttpResponse:
        data = form.cleaned_data
        contact = services.create_contact(
            organization=self.organization,
            name=data["name"],
            phone_number=data["phone_number"],
            email=data.get("email", ""),
            status=data["status"],
            opted_in=data.get("opted_in", False),
            opt_in_source=OptInSource.MANUAL if data.get("opted_in") else "",
            notes=data.get("notes", ""),
            groups=list(data.get("groups", [])),
            user=self.request.user,
            request=self.request,
        )
        messages.success(self.request, f"Contact {contact.name} created.")
        return redirect("contacts:detail", pk=contact.pk)


class ContactUpdateView(CapabilityRequiredMixin, PageTitleMixin, UpdateView):
    model = Contact
    form_class = ContactForm
    template_name = "contacts/contact_form.html"
    required_capability = MANAGE_CONTACTS
    active_nav = "contacts"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit {self.object.name}"
        return context

    def form_valid(self, form) -> HttpResponse:
        data = form.cleaned_data
        contact = services.update_contact(
            self.object,
            name=data["name"],
            phone_number=data["phone_number"],
            email=data.get("email", ""),
            status=data["status"],
            notes=data.get("notes", ""),
            user=self.request.user,
            request=self.request,
        )
        services.set_contact_groups(contact, list(data.get("groups", [])), user=self.request.user)
        messages.success(self.request, f"Contact {contact.name} updated.")
        return redirect("contacts:detail", pk=contact.pk)


class ContactDeleteView(CapabilityRequiredMixin, PageTitleMixin, DeleteView):
    model = Contact
    template_name = "contacts/contact_confirm_delete.html"
    success_url = reverse_lazy("contacts:list")
    required_capability = MANAGE_CONTACTS
    page_title = "Delete contact"
    active_nav = "contacts"

    def form_valid(self, form) -> HttpResponse:
        contact = self.get_object()
        name = contact.name
        services.delete_contact(contact, user=self.request.user, request=self.request)
        messages.success(self.request, f"Contact {name} deleted.")
        return redirect(self.success_url)


class ContactConsentView(CapabilityRequiredMixin, View):
    """POST-only consent toggle, so it cannot be triggered by a link."""

    required_capability = MANAGE_CONTACTS

    def post(self, request: HttpRequest, pk, action: str) -> HttpResponse:
        contact = get_object_or_404(Contact, pk=pk)
        opted_in = action == "opt-in"

        services.set_consent(
            contact,
            opted_in=opted_in,
            source=OptInSource.MANUAL if opted_in else OptOutSource.MANUAL,
            user=request.user,
            request=request,
        )
        messages.success(
            request,
            f"{contact.name} has been {'opted in' if opted_in else 'opted out'}.",
        )
        return redirect("contacts:detail", pk=contact.pk)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class GroupListView(ActiveUserRequiredMixin, PageTitleMixin, ListView):
    model = ContactGroup
    template_name = "contacts/group_list.html"
    context_object_name = "groups"
    paginate_by = 25
    page_title = "Groups"
    active_nav = "groups"

    def get_queryset(self):
        queryset = self.scoped(ContactGroup).with_counts().order_by("name")

        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search"] = self.request.GET.get("search", "")
        return context


class GroupDetailView(ActiveUserRequiredMixin, PageTitleMixin, DetailView):
    model = ContactGroup
    template_name = "contacts/group_detail.html"
    context_object_name = "group"
    active_nav = "groups"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        group = self.object
        context["page_title"] = group.name

        memberships = group.memberships.select_related("contact").order_by("contact__name")
        paginator = Paginator(memberships, 25)
        paginator_page = self.request.GET.get("page", 1)
        context["page_obj"] = paginator.get_page(paginator_page)
        context["memberships"] = context["page_obj"].object_list
        context["is_paginated"] = context["page_obj"].has_other_pages()
        context["member_count"] = paginator.count
        context["eligible_count"] = group.count_eligible()
        return context


class GroupCreateView(CapabilityRequiredMixin, PageTitleMixin, CreateView):
    model = ContactGroup
    form_class = ContactGroupForm
    template_name = "contacts/group_form.html"
    required_capability = MANAGE_CONTACTS
    page_title = "Create group"
    active_nav = "groups"

    def form_valid(self, form) -> HttpResponse:
        form.instance.created_by = self.request.user
        form.instance.organization = self.organization
        response = super().form_valid(form)
        messages.success(self.request, f"Group {self.object.name} created.")
        return response

    def get_success_url(self) -> str:
        return reverse("contacts:group-detail", kwargs={"pk": self.object.pk})


class GroupUpdateView(CapabilityRequiredMixin, PageTitleMixin, UpdateView):
    model = ContactGroup
    form_class = ContactGroupForm
    template_name = "contacts/group_form.html"
    required_capability = MANAGE_CONTACTS
    active_nav = "groups"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Edit {self.object.name}"
        return context

    def get_success_url(self) -> str:
        messages.success(self.request, f"Group {self.object.name} updated.")
        return reverse("contacts:group-detail", kwargs={"pk": self.object.pk})


class GroupDeleteView(CapabilityRequiredMixin, PageTitleMixin, DeleteView):
    model = ContactGroup
    template_name = "contacts/group_confirm_delete.html"
    success_url = reverse_lazy("contacts:group-list")
    required_capability = MANAGE_CONTACTS
    page_title = "Delete group"
    active_nav = "groups"

    def form_valid(self, form) -> HttpResponse:
        name = self.get_object().name
        response = super().form_valid(form)
        messages.success(self.request, f"Group {name} deleted. The contacts themselves were kept.")
        return response


class GroupMembersView(CapabilityRequiredMixin, View):
    """Add or remove group members from the group detail page."""

    required_capability = MANAGE_CONTACTS

    def post(self, request: HttpRequest, pk) -> HttpResponse:
        group = get_object_or_404(ContactGroup, pk=pk)
        action = request.POST.get("action")
        contact_ids = request.POST.getlist("contact_ids")
        contacts = list(Contact.objects.filter(pk__in=contact_ids))

        if not contacts:
            messages.warning(request, "No contacts were selected.")
        elif action == "add":
            services.add_contacts_to_group(group, contacts, user=request.user, request=request)
            messages.success(request, f"Added {len(contacts)} contact(s) to {group.name}.")
        elif action == "remove":
            removed = services.remove_contacts_from_group(group, contacts)
            messages.success(request, f"Removed {removed} contact(s) from {group.name}.")
        else:
            messages.error(request, "Unknown action.")

        return redirect("contacts:group-detail", pk=group.pk)


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


class ContactImportView(CapabilityRequiredMixin, PageTitleMixin, FormView):
    """Upload a CSV file and land on its report."""

    template_name = "contacts/import_form.html"
    form_class = ContactImportForm
    required_capability = MANAGE_CONTACTS
    page_title = "Import contacts"
    active_nav = "contacts"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["recent_imports"] = ContactImport.objects.select_related("target_group")[:5]
        return context

    def form_valid(self, form) -> HttpResponse:
        try:
            contact_import = import_contacts_from_file(
                form.cleaned_data["file"],
                organization=self.organization,
                update_existing=form.cleaned_data.get("update_existing", False),
                target_group=form.cleaned_data.get("target_group"),
                user=self.request.user,
                request=self.request,
            )
        except CsvImportError as exc:
            form.add_error("file", exc.message)
            for message in exc.details.get("file", []):
                form.add_error(None, message)
            return self.form_invalid(form)

        messages.success(
            self.request,
            f"Imported {contact_import.imported_count} contact(s) from {contact_import.file_name}.",
        )
        return redirect("contacts:import-detail", pk=contact_import.pk)


class ContactImportDetailView(ActiveUserRequiredMixin, PageTitleMixin, DetailView):
    """The import summary and the list of rejected rows."""

    model = ContactImport
    template_name = "contacts/import_detail.html"
    context_object_name = "contact_import"
    active_nav = "contacts"

    def get_queryset(self):
        return self.scoped(ContactImport).select_related("target_group", "uploaded_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = f"Import: {self.object.file_name}"
        rows = self.object.rows.all()
        context["invalid_rows"] = [r for r in rows if r.outcome == RowOutcome.INVALID]
        context["duplicate_rows"] = [r for r in rows if r.outcome == RowOutcome.DUPLICATE]
        return context


class ContactImportListView(ActiveUserRequiredMixin, PageTitleMixin, ListView):
    model = ContactImport
    template_name = "contacts/import_list.html"
    context_object_name = "imports"
    paginate_by = 25
    page_title = "Import history"
    active_nav = "contacts"

    def get_queryset(self):
        return self.scoped(ContactImport).select_related("target_group", "uploaded_by")


class SampleCsvView(ActiveUserRequiredMixin, TemplateView):
    """Serves a small example file so operators can see the expected format."""

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        content = (
            "name,phone_number,email,opted_in\n"
            "Aarav Sharma,+9779800000000,aarav@example.com,true\n"
            "Sita Rai,+9779811111111,,yes\n"
            "Bikash Thapa,+9779822222222,bikash@example.com,false\n"
        )
        response = HttpResponse(content, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="sample_contacts.csv"'
        return response
