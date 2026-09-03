"""HTML authentication views."""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import FormView

from accounts import registration, throttling
from accounts.forms import EmailAuthenticationForm, ProfileForm, RegistrationForm
from core.audit import record_audit
from core.mixins import ActiveUserRequiredMixin, PageTitleMixin
from core.models import AuditAction

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


class RegisterView(FormView):
    """
    Self-service sign-up.

    The account is usable immediately and the address is confirmed afterwards.
    Blocking sign-in until a link is clicked strands anyone whose mail is slow
    or filtered, with nothing to look at and no way to ask for help — whereas
    the thing verification actually protects against is sending messages from
    an address nobody can receive replies at, and that is gated at launch
    instead. See ``campaigns.services.launch_campaign``.
    """

    template_name = "accounts/register.html"
    form_class = RegistrationForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("dashboard:home")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """
        Refuse once this network has created its allowance of accounts.

        Registration is the only unauthenticated endpoint that both writes rows
        and sends mail to an address the caller chose, which makes it a way to
        fill the database and a way to point our mail server at somebody who
        never asked for it. The form comes back with what they typed still in
        it, because the overwhelmingly likely person to hit this is an office
        signing colleagues up from one address.
        """
        if throttling.signup.is_exceeded(request):
            logger.warning("Blocked a registration from a throttled address.")
            messages.error(request, throttling.signup.message())
            return self.render_to_response(self.get_context_data(form=self.get_form()))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Create your account"
        return context

    def form_valid(self, form):
        data = form.cleaned_data
        user, organization = registration.register(
            email=data["email"],
            password=data["password1"],
            organization_name=data["organization_name"],
            first_name=data["first_name"],
            last_name=data.get("last_name", ""),
            phone=data.get("phone", ""),
            request=self.request,
        )
        registration.send_verification_email(user, self.request)
        throttling.signup.record(self.request)

        login(self.request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(
            self.request,
            f"Welcome to {settings.SITE_NAME}. We have sent a link to {user.email} — "
            "confirm your address to start sending campaigns.",
        )
        return redirect("dashboard:home")


class VerifyEmailView(View):
    """Confirms an address from the emailed link."""

    def get(self, request, uidb64: str, token: str):
        user = registration.verify(uidb64, token)

        if user is None:
            messages.error(
                request,
                "That confirmation link is invalid or has already been used. "
                "Sign in and request a new one.",
            )
            return redirect("accounts:login")

        record_audit(
            AuditAction.EMAIL_VERIFIED,
            user=user,
            request=request,
            description="Confirmed their email address",
        )
        messages.success(request, "Your email address is confirmed. You can now send campaigns.")
        return redirect("dashboard:home" if request.user.is_authenticated else "accounts:login")


class ResendVerificationView(ActiveUserRequiredMixin, View):
    """
    Sends the link again, for the ordinary case of it never arriving.

    POST-only: a link that triggers an email on GET can be fired by a prefetch
    or a scanner.
    """

    def post(self, request):
        if request.user.email_verified:
            messages.info(request, "Your email address is already confirmed.")
        elif throttling.outbound_email.is_exceeded(request):
            messages.error(request, throttling.outbound_email.message())
        else:
            registration.send_verification_email(request.user, request)
            throttling.outbound_email.record(request)
            messages.success(request, f"We have sent another link to {request.user.email}.")
        return redirect(request.META.get("HTTP_REFERER") or "dashboard:home")


class ThrottledPasswordResetView(auth_views.PasswordResetView):
    """
    Django's reset view, with a cap on how much mail one network can ask for.

    The form is unauthenticated and mails any address that has an account, so
    without a limit it is a way to bury a customer's inbox using our mail server
    and our sending reputation.

    **Counted on every submission, not only the ones that find an account.** A
    counter that advanced only for real addresses would make the block itself
    tell an attacker which addresses are registered — undoing the identical
    response this form goes to the trouble of giving.
    """

    def post(self, request, *args, **kwargs):
        if throttling.outbound_email.is_exceeded(request):
            logger.warning("Blocked a password reset request from a throttled address.")
            messages.error(request, throttling.outbound_email.message())
            return self.render_to_response(self.get_context_data(form=self.get_form()))

        throttling.outbound_email.record(request)
        return super().post(request, *args, **kwargs)
