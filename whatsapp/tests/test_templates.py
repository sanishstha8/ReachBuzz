"""Message templates: variable extraction, rendering, and approval gating."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from core.exceptions import ProviderNotConfigured
from whatsapp.models import MessageTemplate, TemplateSource, TemplateStatus, extract_variables
from whatsapp.services.templates import (
    preview_with_examples,
    render_template,
    substitute,
    sync_templates_from_provider,
)

pytestmark = pytest.mark.django_db


class TestVariableExtraction:
    def test_finds_named_placeholders_in_order(self) -> None:
        assert extract_variables("Hi {{name}}, order {{order_id}} is ready.") == [
            "name",
            "order_id",
        ]

    def test_finds_positional_placeholders(self) -> None:
        assert extract_variables("Hi {{1}}, your code is {{2}}.") == ["1", "2"]

    def test_deduplicates_repeated_placeholders(self) -> None:
        assert extract_variables("{{name}} — thanks {{name}}!") == ["name"]

    def test_tolerates_whitespace_inside_braces(self) -> None:
        assert extract_variables("Hi {{ name }}") == ["name"]

    def test_returns_empty_for_plain_text(self) -> None:
        assert extract_variables("No variables at all.") == []

    def test_ignores_single_braces(self) -> None:
        assert extract_variables("Cost is {100} rupees") == []


class TestTemplateModel:
    def test_variables_are_derived_on_save(self, make_template) -> None:
        """Hand-maintained variable lists drift from the text; derived ones cannot."""
        template = make_template(body_text="Hi {{name}}, ref {{ref}}.")
        assert template.variables == ["name", "ref"]

    def test_variables_update_when_the_body_changes(self, make_template) -> None:
        template = make_template(body_text="Hi {{name}}.")
        template.body_text = "Hi {{name}}, code {{code}}."
        template.save()
        assert template.variables == ["name", "code"]

    def test_header_variables_are_included(self, make_template) -> None:
        template = make_template(body_text="Body {{b}}", header_text="Header {{h}}")
        assert template.variables == ["h", "b"]

    def test_name_and_language_are_unique_together(self, make_template, organization) -> None:
        make_template("promo", language="en_US")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MessageTemplate.objects.create(name="promo", language="en_US", body_text="x", organization=organization)

    def test_same_name_in_another_language_is_allowed(self, make_template) -> None:
        make_template("promo", language="en_US")
        make_template("promo", language="ne_NP")
        assert MessageTemplate.objects.filter(name="promo").count() == 2


class TestUsability:
    """The guard that stops an unapproved template reaching real recipients."""

    def test_approved_synced_template_is_usable_under_meta(self, approved_template) -> None:
        assert approved_template.usability("meta").usable is True

    def test_approved_synced_template_is_usable_under_mock(self, approved_template) -> None:
        assert approved_template.usability("mock").usable is True

    def test_pending_synced_template_is_refused(self, make_template) -> None:
        template = make_template(
            source=TemplateSource.SYNCED, status=TemplateStatus.PENDING
        )
        usability = template.usability("meta")
        assert usability.usable is False
        assert "not approved" in usability.reason.lower()

    def test_local_template_works_with_the_mock_provider(self, local_template) -> None:
        assert local_template.usability("mock").usable is True

    def test_local_template_is_refused_by_the_live_provider(self, local_template) -> None:
        usability = local_template.usability("meta")
        assert usability.usable is False
        assert "WhatsApp Manager" in usability.reason

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_provider_defaults_to_the_setting(self, local_template) -> None:
        assert local_template.is_usable() is False

    def test_usable_with_queryset_filters_by_provider(
        self, approved_template, local_template, make_template
    ) -> None:
        make_template(source=TemplateSource.SYNCED, status=TemplateStatus.REJECTED)

        assert set(MessageTemplate.objects.usable_with("mock")) == {
            approved_template,
            local_template,
        }
        assert set(MessageTemplate.objects.usable_with("meta")) == {approved_template}


class TestSubstitution:
    def test_replaces_known_tokens(self) -> None:
        text, missing = substitute("Hi {{name}}", {"name": "Aarav"})
        assert text == "Hi Aarav"
        assert missing == []

    def test_leaves_unresolved_tokens_visible(self) -> None:
        """Blanking a missing value would silently produce 'Hello ,'."""
        text, missing = substitute("Hi {{name}}, ref {{ref}}", {"name": "Aarav"})
        assert text == "Hi Aarav, ref {{ref}}"
        assert missing == ["ref"]

    def test_empty_string_counts_as_missing(self) -> None:
        _text, missing = substitute("Hi {{name}}", {"name": ""})
        assert missing == ["name"]

    def test_replaces_every_occurrence(self) -> None:
        text, _ = substitute("{{n}} and {{n}}", {"n": "x"})
        assert text == "x and x"


class TestRendering:
    def test_renders_all_parts(self, make_template) -> None:
        template = make_template(
            body_text="Order {{order_id}} ready.",
            header_text="Hello {{name}}",
            footer_text="Reply STOP to opt out",
        )

        rendered = render_template(template, {"name": "Aarav", "order_id": "A-1"})

        assert rendered.header == "Hello Aarav"
        assert rendered.body == "Order A-1 ready."
        assert rendered.footer == "Reply STOP to opt out"
        assert rendered.is_complete

    def test_full_text_joins_the_parts(self, make_template) -> None:
        template = make_template(body_text="Body", header_text="Head", footer_text="Foot")
        assert render_template(template, {}).full_text == "Head\n\nBody\n\nFoot"

    def test_missing_values_are_reported(self, make_template) -> None:
        template = make_template(body_text="Hi {{name}} ref {{ref}}")
        rendered = render_template(template, {"name": "Aarav"})

        assert rendered.missing == ["ref"]
        assert rendered.is_complete is False

    def test_example_preview_fills_every_placeholder(self, make_template) -> None:
        template = make_template(body_text="Hi {{name}}, ref {{ref}}")
        rendered = preview_with_examples(template)

        assert rendered.is_complete
        assert "{{" not in rendered.full_text


class TestSync:
    def test_sync_raises_until_a_provider_is_integrated(self, organization) -> None:
        """Returning 0 would look like 'no templates' rather than 'not wired up'."""
        with pytest.raises(ProviderNotConfigured):
            sync_templates_from_provider(organization=organization)
