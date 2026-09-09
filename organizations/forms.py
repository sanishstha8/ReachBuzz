"""Forms for team membership: sending an invitation, and accepting one."""

from __future__ import annotations

from django import forms
from django.contrib.auth import password_validation

from organizations.invitations import INVITABLE_ROLES
from organizations.models import OrganizationRole


class InviteMemberForm(forms.Form):
    """The form on the members page. Role is restricted to what may be sent."""

    email = forms.EmailField(
        label="Email address", widget=forms.EmailInput(attrs={"class": "form-control"})
    )
    role = forms.ChoiceField(
        label="Role",
        choices=[
            (value, label) for value, label in OrganizationRole.choices if value in INVITABLE_ROLES
        ],
        initial=OrganizationRole.MEMBER,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().lower()


class AcceptInvitationForm(forms.Form):
    """
    For a stranger accepting an invitation with no account yet.

    Not shown at all to somebody already signed in as the invited address —
    ``AcceptInvitationView`` skips straight to accepting for them, since
    asking somebody to set a password for an account they already have would
    be nonsensical.
    """

    first_name = forms.CharField(
        label="First name", max_length=150, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    last_name = forms.CharField(
        label="Last name",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        strip=False,
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        strip=False,
    )

    def clean(self) -> dict:
        cleaned = super().clean()
        password1, password2 = cleaned.get("password1"), cleaned.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two passwords do not match.")
        elif password1:
            # Django's configured validators, so an invited colleague meets
            # the same bar as somebody who registered directly.
            try:
                password_validation.validate_password(password1)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned
