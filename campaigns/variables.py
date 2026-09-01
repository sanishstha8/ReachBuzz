"""
Template variable resolution.

A campaign maps each placeholder in its template to either a fixed string or a
field on the recipient. Contact fields come from an explicit allow-list rather
than ``getattr``: an operator-supplied field name must never be able to reach
``password``, a related manager, or an arbitrary attribute.
"""

from __future__ import annotations

from dataclasses import dataclass

from contacts.models import Contact
from core.exceptions import ValidationFailed

# The only contact attributes a template variable may read.
ALLOWED_CONTACT_FIELDS: dict[str, str] = {
    "name": "Name",
    "phone_number": "Phone number",
    "email": "Email address",
    "country_code": "Country dialling code",
}


@dataclass(frozen=True)
class VariableBinding:
    """How one placeholder gets its value."""

    token: str
    source: str
    value: str

    @property
    def is_contact_field(self) -> bool:
        return self.source == "contact_field"


def parse_mapping(mapping: dict) -> dict[str, VariableBinding]:
    """Turn the stored JSON mapping into validated bindings."""
    bindings: dict[str, VariableBinding] = {}

    for token, spec in (mapping or {}).items():
        if not isinstance(spec, dict):
            continue
        source = spec.get("source") or "literal"
        value = spec.get("value") or ""
        bindings[str(token)] = VariableBinding(token=str(token), source=source, value=str(value))

    return bindings


def validate_mapping(mapping: dict, required_tokens: list[str]) -> None:
    """
    Check a mapping covers every placeholder and reads only allowed fields.

    Raises :class:`~core.exceptions.ValidationFailed` with per-token detail so
    the wizard can highlight the offending row.
    """
    bindings = parse_mapping(mapping)
    errors: dict[str, list[str]] = {}

    for token in required_tokens:
        binding = bindings.get(token)
        if binding is None or not binding.value:
            errors[token] = ["A value is required for this variable."]
            continue

        if binding.source not in ("contact_field", "literal"):
            errors[token] = [f"Unknown variable source '{binding.source}'."]
            continue

        if binding.is_contact_field and binding.value not in ALLOWED_CONTACT_FIELDS:
            allowed = ", ".join(sorted(ALLOWED_CONTACT_FIELDS))
            errors[token] = [f"'{binding.value}' is not a permitted contact field. Use one of: {allowed}."]

    if errors:
        raise ValidationFailed(
            "Some template variables are not correctly mapped.", details=errors
        )


def resolve_values(mapping: dict, contact: Contact) -> dict[str, str]:
    """Resolve every placeholder to a concrete string for ``contact``."""
    values: dict[str, str] = {}

    for token, binding in parse_mapping(mapping).items():
        if binding.is_contact_field:
            if binding.value not in ALLOWED_CONTACT_FIELDS:
                # Defence in depth: validate_mapping should have caught this.
                values[token] = ""
                continue
            values[token] = str(getattr(contact, binding.value, "") or "")
        else:
            values[token] = binding.value

    return values


def sample_contact_values(mapping: dict) -> dict[str, str]:
    """Placeholder values for a preview when no real recipient is available."""
    labels = {
        "name": "Aarav Sharma",
        "phone_number": "+9779800000000",
        "email": "aarav@example.com",
        "country_code": "977",
    }
    values: dict[str, str] = {}

    for token, binding in parse_mapping(mapping).items():
        if binding.is_contact_field:
            values[token] = labels.get(binding.value, f"[{binding.value}]")
        else:
            values[token] = binding.value

    return values
