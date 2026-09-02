"""
Template rendering and the sync abstraction.

Rendering is deliberately dumb: substitute known placeholders, escape nothing,
invent nothing. What the preview shows is exactly the text that will be handed
to the provider, so an operator can trust the confirmation screen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

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


@transaction.atomic
def sync_templates_from_provider(*, user=None, request=None) -> int:
    """
    Mirror the provider's template registry into our own.

    This is the one place the application is allowed to write a template's
    approval status, and it writes only what Meta reports. Nothing here can
    decide a template is approved, and nothing submits one for review —
    approval is Meta's to grant, in WhatsApp Manager.

    A simulated provider is refused rather than synced. The mock has no
    upstream registry, so a sync would return zero, and "0 templates synced"
    reads as *you have none* rather than *there was nothing to ask*.
    """
    from core.audit import record_audit
    from core.models import AuditAction
    from whatsapp.services.factory import get_provider, is_simulated, provider_name

    if is_simulated():
        raise ProviderNotConfigured(
            f"Template sync is not available for the '{provider_name()}' provider. "
            "The mock has no upstream registry; create local templates for development, "
            "or set WHATSAPP_PROVIDER=meta with credentials to sync real ones."
        )

    fetched = get_provider().fetch_templates()
    synced = 0

    for data in fetched:
        if not data.name:
            logger.warning("Skipping a template with no name in the provider response.")
            continue
        _apply_template(data, user=user)
        synced += 1

    record_audit(
        AuditAction.TEMPLATES_SYNCED,
        user=user,
        request=request,
        description=f"Synced {synced} template(s) from the provider",
        metadata={"count": synced, "provider": provider_name()},
    )
    logger.info("Synced %d template(s) from the provider.", synced)
    return synced


def _apply_template(data, *, user=None) -> MessageTemplate:
    """
    Create or update the local mirror of one provider template.

    Matched on (name, language), which is the pair Meta itself treats as
    identifying — and the pair our own unique constraint is built on.

    A local development template that collides with a real one is converted
    rather than skipped: once Meta has a template by that name, Meta's version
    is the truth, and leaving a local stub shadowing it would let someone send
    a draft believing it was approved.
    """
    from whatsapp.models import TemplateSource, extract_variables

    body = data.body_text or ""
    defaults = {
        "category": data.category or "utility",
        "source": TemplateSource.SYNCED,
        "status": data.status,
        "body_text": body,
        "header_text": data.header_text or "",
        "footer_text": data.footer_text or "",
        "variables": extract_variables(body),
        "provider_template_id": data.provider_template_id or "",
        "rejection_reason": data.rejection_reason or "",
        "synced_at": timezone.now(),
    }

    template, created = MessageTemplate.objects.update_or_create(
        name=data.name,
        language=data.language or "",
        defaults=defaults,
    )
    if created and user is not None:
        MessageTemplate.objects.filter(pk=template.pk).update(created_by=user)

    return template
