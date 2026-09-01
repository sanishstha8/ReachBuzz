"""Forms for the contact management pages."""

from __future__ import annotations

from django import forms

from contacts.models import Contact, ContactGroup, ContactStatus
from contacts.services import find_duplicate, normalize_contact_phone
from core.exceptions import ValidationFailed


class ContactForm(forms.ModelForm):
    """
    Create/edit form.

    Phone numbers are normalized in ``clean_phone_number`` so that duplicate
    detection compares like with like, and the initial consent decision is an
    explicit, separately-labelled choice rather than a checkbox buried in the
    form.
    """

    groups = forms.ModelMultipleChoiceField(
        queryset=ContactGroup.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 6}),
        help_text="Hold Ctrl (Cmd on Mac) to select more than one.",
    )
    opted_in = forms.BooleanField(
        required=False,
        label="This contact has given consent to receive WhatsApp messages",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Only tick this when consent has genuinely been obtained and can be evidenced.",
    )

    class Meta:
        model = Contact
        fields = ("name", "phone_number", "email", "status", "notes")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "autocomplete": "name"}),
            "phone_number": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "+9779800000000",
                    "inputmode": "tel",
                    "autocomplete": "tel",
                }
            ),
            "email": forms.EmailInput(attrs={"class": "form-control", "autocomplete": "email"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = ContactStatus.choices
        self.fields["email"].required = False

        # NB: `self.instance.pk` is *not* a reliable "is this saved?" test here.
        # Contact's primary key has a uuid4 default, so an unsaved instance
        # already has a pk. `_state.adding` is the correct check — getting this
        # wrong disabled the consent checkbox on the create form.
        is_existing = not self.instance._state.adding

        if is_existing:
            self.fields["groups"].initial = ContactGroup.objects.filter(
                memberships__contact=self.instance
            )
            self.fields["opted_in"].initial = self.instance.opted_in
            # Consent changes on an existing contact go through the dedicated
            # opt-in/opt-out action, which records a source and an audit entry.
            self.fields["opted_in"].disabled = True
            self.fields["opted_in"].help_text = (
                "Use the opt-in / opt-out buttons on the contact page to change consent."
            )

    def clean_phone_number(self) -> str:
        raw = self.cleaned_data["phone_number"]
        try:
            e164, _ = normalize_contact_phone(raw)
        except ValidationFailed as exc:
            raise forms.ValidationError(exc.message) from exc

        exclude_pk = None if self.instance._state.adding else self.instance.pk
        duplicate = find_duplicate(e164, exclude_pk=exclude_pk)
        if duplicate is not None:
            raise forms.ValidationError(
                f"{duplicate.name} already uses {e164}. Edit that contact instead."
            )
        return e164

    def clean_name(self) -> str:
        return (self.cleaned_data.get("name") or "").strip()


class ContactGroupForm(forms.ModelForm):
    class Meta:
        model = ContactGroup
        fields = ("name", "description")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def clean_name(self) -> str:
        return (self.cleaned_data.get("name") or "").strip()


class ContactImportForm(forms.Form):
    """CSV upload options."""

    file = forms.FileField(
        label="CSV file",
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".csv"}),
        help_text="Required columns: name, phone_number. Optional: email, opted_in, notes.",
    )
    target_group = forms.ModelChoiceField(
        queryset=ContactGroup.objects.all(),
        required=False,
        empty_label="— none —",
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Optionally add every imported contact to this group.",
    )
    update_existing = forms.BooleanField(
        required=False,
        label="Update contacts that already exist",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text=(
            "Off by default: rows whose number already exists are reported as duplicates "
            "and skipped. Consent is never revoked by an import."
        ),
    )
    confirm_consent = forms.BooleanField(
        required=True,
        label="I confirm every contact marked as opted in has genuinely consented to receive messages",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
