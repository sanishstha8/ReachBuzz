"""
The members page: who is in the organization, and inviting more of them.

Same split as the billing area. Any member can see who else is on the team;
only an owner or administrator can invite, revoke, or remove — reusing
``OrganizationMember.can_administer`` rather than inventing a second notion of
who is in charge.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView, TemplateView

from core.exceptions import DomainError
from core.mixins import ActiveUserRequiredMixin, PageTitleMixin
from organizations import invitations
from organizations.forms import AcceptInvitationForm, InviteMemberForm
from organizations.models import Invitation, OrganizationMember

logger = logging.getLogger(__name__)


class MembersAccessMixin(ActiveUserRequiredMixin):
    """Adds ``can_manage_members`` to the view and the template context."""

    active_nav = "members"

    @property
    def can_manage_members(self) -> bool:
        if not getattr(self, "organization", None):
            return False
        membership = OrganizationMember.objects.filter(
            organization=self.organization, user=self.request.user
        ).first()
        return bool(membership and membership.can_administer)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_manage_members"] = self.can_manage_members
        context["organization"] = self.organization
        return context


class MembersRequiredMixin(MembersAccessMixin):
    """For the pages that change something. Refuses, rather than hiding."""

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if request.user.is_authenticated:
            from organizations.scoping import organization_for

            self.organization = organization_for(request.user)
            if not self.can_manage_members:
                messages.error(
                    request, "Only an owner or administrator can manage the team."
                )
                return redirect("organizations:members")
        return super().dispatch(request, *args, **kwargs)


class MemberListView(MembersAccessMixin, PageTitleMixin, TemplateView):
    template_name = "organizations/members.html"
    page_title = "Team"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["members"] = (
            OrganizationMember.objects.filter(organization=self.organization)
            .select_related("user")
            .order_by("role", "user__email")
        )
        context["pending_invitations"] = (
            Invitation.objects.for_organization(self.organization)
            .pending()
            .select_related("invited_by")
        )
        context["invite_form"] = InviteMemberForm()
        return context


class InviteMemberView(MembersRequiredMixin, View):
    """
    POST only. Sending an invitation changes state and mails somebody.

    An invalid or refused form redirects back to the list with the reason as a
    flash message, rather than re-rendering the page in place — this is one
    field on a list page, not a wizard step, and the same shape the billing
    views already use for their action endpoints.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        form = InviteMemberForm(request.POST)
        if not form.is_valid():
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, error if field == "__all__" else f"{field}: {error}")
            return redirect("organizations:members")

        try:
            invitation = invitations.invite(
                self.organization,
                email=form.cleaned_data["email"],
                role=form.cleaned_data["role"],
                invited_by=request.user,
                request=request,
            )
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("organizations:members")

        messages.success(request, f"Invited {invitation.email}.")
        return redirect("organizations:members")


class ResendInvitationView(MembersRequiredMixin, View):
    def post(self, request: HttpRequest, pk) -> HttpResponse:
        invitation = get_object_or_404(
            Invitation.objects.pending(), pk=pk, organization=self.organization
        )
        invitations.send_invitation_email(invitation, request=request)
        messages.success(request, f"Sent the invitation to {invitation.email} again.")
        return redirect("organizations:members")


class RevokeInvitationView(MembersRequiredMixin, View):
    def post(self, request: HttpRequest, pk) -> HttpResponse:
        invitation = get_object_or_404(Invitation, pk=pk, organization=self.organization)
        invitations.revoke(invitation, user=request.user, request=request)
        messages.success(request, f"Revoked the invitation to {invitation.email}.")
        return redirect("organizations:members")


class RemoveMemberView(MembersRequiredMixin, View):
    def post(self, request: HttpRequest, pk) -> HttpResponse:
        member = get_object_or_404(
            OrganizationMember, pk=pk, organization=self.organization
        )
        try:
            invitations.remove_member(member, removed_by=request.user, request=request)
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("organizations:members")

        messages.success(request, "Removed from the team.")
        return redirect("organizations:members")


class AcceptInvitationView(PageTitleMixin, DetailView):
    """
    Public. Reached from the emailed link, by someone who may not be signed in.

    GET shows what the invitation offers and, for a stranger, a short form to
    create an account. POST does the accepting. A signed-in visitor whose own
    address does not match the invitation is told to sign out first, rather
    than the invitation silently doing nothing for them.
    """

    template_name = "organizations/accept_invitation.html"
    page_title = "Join the team"
    context_object_name = "invitation"

    def get_object(self, queryset=None) -> Invitation:
        invitation = invitations.find_open(self.kwargs["token"])
        if invitation is None:
            from django.http import Http404

            raise Http404("This invitation is no longer open.")
        return invitation

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invitation = context["invitation"]
        context["existing_user"] = self._existing_user(invitation.email)
        context["signed_in_as_someone_else"] = (
            self.request.user.is_authenticated
            and self.request.user.email.lower() != invitation.email.lower()
        )
        context.setdefault("form", AcceptInvitationForm())
        return context

    def _existing_user(self, email: str):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(email__iexact=email).first()

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # DetailView.get_context_data reads self.object; only get() sets it
        # for us, so a POST has to do it itself before rendering the form back.
        self.object = invitation = self.get_object()

        if request.user.is_authenticated:
            if request.user.email.lower() != invitation.email.lower():
                messages.error(
                    request,
                    f"You are signed in as {request.user.email}. Sign out to accept an "
                    f"invitation sent to {invitation.email}.",
                )
                return redirect("organizations:invitation-accept", token=invitation.token)
            return self._accept(request, invitation, user=request.user)

        existing = self._existing_user(invitation.email)
        if existing is not None:
            messages.info(request, "Sign in to accept this invitation.")
            return redirect(f"{reverse_login()}?next={request.path}")

        form = AcceptInvitationForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data(form=form)
            return self.render_to_response(context)

        return self._accept(
            request,
            invitation,
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            password=form.cleaned_data["password1"],
        )

    def _accept(self, request, invitation, **kwargs) -> HttpResponse:
        from django.contrib.auth import login

        try:
            member = invitations.accept(invitation, request=request, **kwargs)
        except DomainError as exc:
            messages.error(request, exc.message)
            return redirect("organizations:invitation-accept", token=invitation.token)

        if not request.user.is_authenticated:
            login(request, member.user, backend="django.contrib.auth.backends.ModelBackend")

        messages.success(request, f"Welcome to {invitation.organization.name}.")
        return redirect("dashboard:home")


def reverse_login() -> str:
    from django.urls import reverse

    return reverse("accounts:login")
