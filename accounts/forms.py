"""Authentication forms."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model, password_validation
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


class RegistrationForm(forms.Form):
    """
    Self-service sign-up.

    A plain Form rather than a ModelForm: registering creates a user *and* an
    organization, so there is no single model for it to be a form of, and
    pretending otherwise would put the organization's name somewhere it does
    not belong.
    """

    organization_name = forms.CharField(
        label="Business name",
        max_length=150,
        help_text="How your business appears inside the platform.",
    )
    first_name = forms.CharField(label="First name", max_length=150)
    last_name = forms.CharField(label="Last name", max_length=150, required=False)
    email = forms.EmailField(label="Work email")
    phone = forms.CharField(
        label="Phone number",
        max_length=20,
        required=False,
        help_text="Optional. How we reach you about your account.",
    )
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, strip=False)
    password2 = forms.CharField(
        label="Confirm password", widget=forms.PasswordInput, strip=False
    )

    def clean_email(self) -> str:
        """
        Normalise, then check the address is free.

        Stored lowercase by the manager, so the check has to be too — otherwise
        two accounts differing only in case would collide at the database and
        raise where a form error belongs.
        """
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account already uses this email address.")
        return email

    def clean_phone(self) -> str:
        """Normalise to E.164 when it parses; keep what was typed when it does not."""
        phone = (self.cleaned_data.get("phone") or "").strip()
        if not phone:
            return ""
        from core.phone import PhoneNumberError, normalize_phone_number

        try:
            return normalize_phone_number(phone)
        except PhoneNumberError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean(self) -> dict:
        cleaned = super().clean()
        password1, password2 = cleaned.get("password1"), cleaned.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two passwords do not match.")
        elif password1:
            # Django's configured validators, so the rules stated on the
            # sign-in side are the same ones applied here.
            try:
                password_validation.validate_password(password1)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned
