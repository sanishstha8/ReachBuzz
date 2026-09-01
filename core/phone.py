"""
Phone number parsing, validation and normalization.

Every phone number stored by this project is kept in E.164 form
(``+9779800000000``). Normalizing on the way in is what makes duplicate
detection, webhook matching and provider payloads reliable: the same person
typed as ``0980-000 0000`` and ``+977 980 0000000`` must collapse to one row.

A valid E.164 number is *not* a guarantee that the number is reachable on
WhatsApp. That is only ever confirmed by the provider at send time.
"""

from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
from django.conf import settings
from django.core.exceptions import ValidationError
from phonenumbers import NumberParseException


class PhoneNumberError(ValueError):
    """Raised when a phone number cannot be parsed or is not a valid number."""


@dataclass(frozen=True)
class ParsedPhoneNumber:
    """Normalized representation of a phone number."""

    e164: str
    country_code: str  # dialling code, e.g. "977"
    national_number: str
    region: str  # ISO 3166-1 alpha-2, e.g. "NP" ("" when undeterminable)

    @property
    def display(self) -> str:
        return phonenumbers.format_number(
            phonenumbers.parse(self.e164, None),
            phonenumbers.PhoneNumberFormat.INTERNATIONAL,
        )


def _default_region() -> str:
    return getattr(settings, "DEFAULT_COUNTRY_CODE", "") or None


def parse_phone_number(value: str, default_region: str | None = None) -> ParsedPhoneNumber:
    """
    Parse ``value`` into a :class:`ParsedPhoneNumber`.

    ``default_region`` is only consulted for numbers written without a leading
    ``+``; it defaults to ``settings.DEFAULT_COUNTRY_CODE``.

    Raises :class:`PhoneNumberError` for anything that is not a valid number.
    """
    if value is None:
        raise PhoneNumberError("Phone number is required.")

    raw = str(value).strip()
    if not raw:
        raise PhoneNumberError("Phone number is required.")

    region = default_region if default_region is not None else _default_region()

    try:
        number = phonenumbers.parse(raw, None if raw.startswith("+") else region)
    except NumberParseException as exc:
        raise PhoneNumberError(_parse_error_message(exc, raw)) from exc

    if not phonenumbers.is_possible_number(number):
        raise PhoneNumberError(f"'{raw}' is not a possible phone number (wrong length).")

    if not phonenumbers.is_valid_number(number):
        raise PhoneNumberError(f"'{raw}' is not a valid phone number.")

    return ParsedPhoneNumber(
        e164=phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164),
        country_code=str(number.country_code),
        national_number=str(number.national_number),
        region=phonenumbers.region_code_for_number(number) or "",
    )


def normalize_phone_number(value: str, default_region: str | None = None) -> str:
    """Return the E.164 form of ``value``, or raise :class:`PhoneNumberError`."""
    return parse_phone_number(value, default_region).e164


def is_valid_phone_number(value: str, default_region: str | None = None) -> bool:
    """Non-raising variant of :func:`parse_phone_number`."""
    try:
        parse_phone_number(value, default_region)
    except PhoneNumberError:
        return False
    return True


def validate_phone_number(value: str) -> None:
    """Django model/form validator wrapping :func:`parse_phone_number`."""
    try:
        parse_phone_number(value)
    except PhoneNumberError as exc:
        raise ValidationError(str(exc), code="invalid_phone_number") from exc


def _parse_error_message(exc: NumberParseException, raw: str) -> str:
    """Turn a phonenumbers error into something a user can act on."""
    messages = {
        NumberParseException.INVALID_COUNTRY_CODE: (
            f"'{raw}' has no recognisable country code. Use international format, e.g. +9779800000000."
        ),
        NumberParseException.NOT_A_NUMBER: f"'{raw}' does not look like a phone number.",
        NumberParseException.TOO_SHORT_NSN: f"'{raw}' is too short to be a phone number.",
        NumberParseException.TOO_SHORT_AFTER_IDD: f"'{raw}' is too short to be a phone number.",
        NumberParseException.TOO_LONG: f"'{raw}' is too long to be a phone number.",
    }
    return messages.get(exc.error_type, f"'{raw}' could not be parsed as a phone number.")
