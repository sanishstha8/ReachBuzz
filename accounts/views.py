"""HTML authentication views."""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import FormView

from accounts import throttling
from accounts.forms import EmailAuthenticationForm, ProfileForm
from core.mixins import ActiveUserRequiredMixin, PageTitleMixin

logger = logging.getLogger(__name__)


class LoginView(auth_views.LoginView):
    """
    Session login. Rotates the session key on success (Django default).

    Repeated failures from one address are throttled. The REST login has been
    rate limited since Phase 2; without this, that limit could be walked around
    by posting to the page a browser uses instead.
    """

    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Sign in"
        return context

    def post(self, request, *args, **kwargs):
        """
        Refuse before authenticating, not after.

        Checking the password first and then declining to act on the result
        would still let an attacker measure it.
        """
        if throttling.is_locked_out(request):
            logger.warning("Blocked a sign-in attempt from a throttled address.")
            form = self.get_form()
            form.add_error(None, throttling.lockout_message())
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        throttling.record_failure(self.request)
        return super().form_invalid(form)

    def form_valid(self, form):
        throttling.reset(self.request)
        response = super().form_valid(form)
        messages.success(self.request, f"Welcome back, {self.request.user.display_name}.")
        return response


class LogoutView(auth_views.LogoutView):
    """POST-only logout, so a stray link or prefetch cannot sign a user out."""

    next_page = reverse_lazy("accounts:login")


class ProfileView(ActiveUserRequiredMixin, PageTitleMixin, FormView):
    """Self-service profile and password change."""

    template_name = "accounts/profile.html"
    form_class = ProfileForm
    success_url = reverse_lazy("accounts:profile")
    page_title = "My profile"
    active_nav = "profile"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("password_form", PasswordChangeForm(user=self.request.user))
        return context

    def post(self, request, *args, **kwargs):
        if "change_password" in request.POST:
            return self._handle_password_change(request)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        form.save()
        messages.success(self.request, "Profile updated.")
        return super().form_valid(form)

    def _handle_password_change(self, request):
        password_form = PasswordChangeForm(user=request.user, data=request.POST)
        if password_form.is_valid():
            user = password_form.save()
            # Keep the current session signed in after the password hash changes.
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed.")
            return redirect(self.success_url)
        # Re-render with an unbound profile form so the password errors show
        # without the profile fields being clobbered by the password POST.
        return self.render_to_response(
            self.get_context_data(
                form=self.form_class(instance=request.user),
                password_form=password_form,
            )
        )
