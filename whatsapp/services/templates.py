"""
Template rendering and the sync abstraction.

Rendering is deliberately dumb: substitute known placeholders, escape nothing,
invent nothing. What the preview shows is exactly the text that will be handed
to the provider, so an operator can trust the confirmation screen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings

from core.exceptions import ProviderNotConfigured, ValidationFailed
from whatsapp.models import VARIABLE_PATTERN, MessageTemplate

logger = logging.getLogger(__name__)


@dataclass
class RenderedTemplate:
    """The concrete text a single recipient would receive."""

    header: str = ""
    body: str = ""
    footer: str = ""
    values: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        parts = [self.header, self.body, self.footer]
        return "\n\n".join(part for part in parts if part)

    @property
    def is_complete(self) -> bool:
        return not self.missing


def substitute(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """
    Replace ``{{token}}`` occurrences in ``text``.

    Returns the rendered text plus the tokens that had no value. Unresolved
    placeholders are left visible rather than blanked, so a missing value is
    obvious in the preview instead of silently producing "Hello ,".
    """
    missing: list[str] = []

    def replace(match) -> str:
        token = match.group(1)
        value = values.get(token)
        if value is None or value == "":
            if token not in missing:
                missing.append(token)
            return match.group(0)
        return str(value)

    return VARIABLE_PATTERN.sub(replace, text or ""), missing


def render_template(template: MessageTemplate, values: dict[str, str]) -> RenderedTemplate:
    """Render every part of ``template`` with ``values``."""
    header, header_missing = substitute(template.header_text, values)
    body, body_missing = substitute(template.body_text, values)

    missing = header_missing + [token for token in body_missing if token not in header_missing]

    return RenderedTemplate(
        header=header,
        body=body,
        footer=template.footer_text,
        values=values,
        missing=missing,
    )


def preview_with_examples(template: MessageTemplate) -> RenderedTemplate:
    """Render using the template's own example values, for the template page."""
    examples = dict(template.example_values or {})
    for index, token in enumerate(template.variables or [], start=1):
        examples.setdefault(token, f"[{token}]" if not token.isdigit() else f"[value {index}]")
    return render_template(template, examples)


def validate_template_text(body_text: str) -> None:
    """Reject template bodies that could not be sent as written."""
    if not (body_text or "").strip():
        raise ValidationFailed(
            "The template body cannot be empty.",
            details={"body_text": ["Enter the message text."]},
        )
    if len(body_text) > 1024:
        raise ValidationFailed(
            "The template body is too long.",
            details={"body_text": ["WhatsApp template bodies are limited to 1024 characters."]},
        )


# ---------------------------------------------------------------------------
# Sync abstraction
# ---------------------------------------------------------------------------


def sync_templates_from_provider(*, user=None) -> int:
    """
    Pull the approved template list from the configured provider.

    Implemented in Phase 5 for the mock provider and Phase 7 against the Meta
    Cloud API, using Meta's documented endpoint at that time. Raising here is
    deliberate: silently returning zero would look like "you have no
    templates" rather than "this is not wired up yet".
    """
    provider = getattr(settings, "WHATSAPP_PROVIDER", "mock")
    raise ProviderNotConfigured(
        f"Template sync is not available for the '{provider}' provider yet. "
        "It is implemented alongside the provider integration; until then, create "
        "local templates for development."
    )
