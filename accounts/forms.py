"""Authentication forms."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserChangeForm, UserCreationForm

User = get_user_model()

BOOTSTRAP_INPUT = "form-control form-control-lg"


class EmailAuthenticationForm(AuthenticationForm):
    """Login form labelled for email, with accessible, Bootstrap-styled inputs."""

    username = forms.EmailField(
        label="Email address",
        widget=forms.EmailInput(
            attrs={
                "class": BOOTSTRAP_INPUT,
                "autofocus": True,
                "autocomplete": "username",
                "placeholder": "you@example.com",
                "id": "id_email",
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": BOOTSTRAP_INPUT,
                "autocomplete": "current-password",
                "placeholder": "••••••••",
                "id": "id_password",
            }
        ),
    )

    error_messages = {
        # Deliberately identical for "no such user" and "wrong password" so the
        # form cannot be used to enumerate valid accounts.
        "invalid_login": "Incorrect email address or password.",
        "inactive": "This account has been deactivated. Contact an administrator.",
    }


class AdminUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "role")


class AdminUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "role", "is_active", "is_staff")


class ProfileForm(forms.ModelForm):
    """Lets a signed-in user edit their own name (not their role)."""

    class Meta:
        model = User
        fields = ("first_name", "last_name")
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
        }
