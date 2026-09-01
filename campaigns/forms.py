"""Forms for the campaign wizard."""

from __future__ import annotations

from django import forms
from django.conf import settings

from campaigns.models import Campaign, CampaignMessageType
from campaigns.variables import ALLOWED_CONTACT_FIELDS
from contacts.models import ContactGroup
from whatsapp.models import MessageTemplate


class CampaignDetailsForm(forms.ModelForm):
    """Step 1 — name the campaign."""

    class Meta:
        model = Campaign
        fields = ("name", "description")
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control form-control-lg", "autofocus": True,
                       "placeholder": "e.g. Summer Sale announcement"}
            ),
            "description": forms.Textarea(
                attrs={"class": "form-control", "rows": 3,
                       "placeholder": "What this campaign is for (optional)"}
            ),
        }

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("A campaign name is required.")
        return name


class CampaignAudienceForm(forms.Form):
    """Step 2 — choose who receives it."""

    target_all_eligible = forms.BooleanField(
        required=False,
        label="Send to every opted-in, active contact",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "id": "id_target_all"}),
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=ContactGroup.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        label="Groups",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = ContactGroup.objects.with_counts().order_by("name")

    def clean(self) -> dict:
        cleaned = super().clean()
        if not cleaned.get("target_all_eligible") and not cleaned.get("groups"):
            raise forms.ValidationError(
                "Select at least one group, or tick 'send to every opted-in contact'."
            )
        return cleaned


class CampaignMessageForm(forms.Form):
    """
    Step 3 — choose the template and map its variables.

    Variable fields are built dynamically from the selected template, so the
    form can only ever ask for placeholders the template actually contains.
    """

    message_type = forms.ChoiceField(
        choices=CampaignMessageType.choices,
        initial=CampaignMessageType.TEMPLATE,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )
    template = forms.ModelChoiceField(
        queryset=MessageTemplate.objects.none(),
        required=False,
        empty_label="— select a template —",
        widget=forms.Select(attrs={"class": "form-select", "id": "id_template"}),
    )
    body_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        label="Message text",
        help_text=(
            "Free-form text can only be delivered inside WhatsApp's 24-hour customer-service "
            "window, which opens when the recipient messages you first."
        ),
    )

    def __init__(self, *args, template=None, **kwargs):
        self.selected_template = template
        super().__init__(*args, **kwargs)

        provider = getattr(settings, "WHATSAPP_PROVIDER", "mock")
        self.fields["template"].queryset = MessageTemplate.objects.usable_with(provider).order_by(
            "name"
        )

        # One field per placeholder in the chosen template.
        self.variable_fields: list[str] = []
        if template is not None:
            contact_choices = [("", "— fixed text —")] + [
                (key, label) for key, label in ALLOWED_CONTACT_FIELDS.items()
            ]
            for token in template.variables or []:
                source_name = f"var_source_{token}"
                value_name = f"var_value_{token}"

                self.fields[source_name] = forms.ChoiceField(
                    choices=contact_choices,
                    required=False,
                    label=f"{{{{{token}}}}} source",
                    widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
                )
                self.fields[value_name] = forms.CharField(
                    required=False,
                    label=f"{{{{{token}}}}} value",
                    widget=forms.TextInput(
                        attrs={"class": "form-control form-control-sm",
                               "placeholder": "Fixed text for every recipient"}
                    ),
                )
                self.variable_fields.append(token)

    def clean(self) -> dict:
        cleaned = super().clean()
        message_type = cleaned.get("message_type")

        if message_type == CampaignMessageType.TEMPLATE:
            if not cleaned.get("template"):
                self.add_error("template", "Select a template.")
                return cleaned

            for token in self.variable_fields:
                source = cleaned.get(f"var_source_{token}") or ""
                literal = (cleaned.get(f"var_value_{token}") or "").strip()
                if not source and not literal:
                    self.add_error(
                        f"var_value_{token}",
                        "Choose a contact field or enter fixed text for this variable.",
                    )
        elif not (cleaned.get("body_text") or "").strip():
            self.add_error("body_text", "Enter the message text.")

        return cleaned

    def variable_mapping(self) -> dict:
        """Build the JSON mapping the campaign stores."""
        mapping: dict[str, dict[str, str]] = {}

        for token in self.variable_fields:
            source = self.cleaned_data.get(f"var_source_{token}") or ""
            literal = (self.cleaned_data.get(f"var_value_{token}") or "").strip()

            if source:
                mapping[token] = {"source": "contact_field", "value": source}
            elif literal:
                mapping[token] = {"source": "literal", "value": literal}

        return mapping

    def variable_rows(self):
        """Zip the paired fields together for the template."""
        for token in self.variable_fields:
            yield token, self[f"var_source_{token}"], self[f"var_value_{token}"]


class CampaignConfirmForm(forms.Form):
    """Step 5 — the explicit go/no-go."""

    confirm = forms.BooleanField(
        required=True,
        label="I confirm this campaign should be sent to the recipients listed above",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class LocalTemplateForm(forms.ModelForm):
    """Create a development-only template while the mock provider is active."""

    class Meta:
        model = MessageTemplate
        fields = ("name", "language", "category", "header_text", "body_text", "footer_text")
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "order_ready"}
            ),
            "language": forms.TextInput(attrs={"class": "form-control", "placeholder": "en_US"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "header_text": forms.TextInput(attrs={"class": "form-control"}),
            "body_text": forms.Textarea(
                attrs={"class": "form-control", "rows": 5,
                       "placeholder": "Hello {{name}}, your order {{order_id}} is ready."}
            ),
            "footer_text": forms.TextInput(attrs={"class": "form-control"}),
        }

    def clean_name(self) -> str:
        return (self.cleaned_data.get("name") or "").strip().lower().replace(" ", "_")

    def clean_body_text(self) -> str:
        body = (self.cleaned_data.get("body_text") or "").strip()
        if len(body) > 1024:
            raise forms.ValidationError(
                "WhatsApp template bodies are limited to 1024 characters."
            )
        return body
