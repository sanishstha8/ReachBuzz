"""Phone normalization is the foundation of duplicate detection, so it is
tested against the shapes real CSV files contain."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings

from core.phone import (
    PhoneNumberError,
    is_valid_phone_number,
    normalize_phone_number,
    parse_phone_number,
    validate_phone_number,
)


class TestNormalization:
    @pytest.mark.parametrize(
        "raw",
        [
            "+9779800000000",
            "+977 980 000 0000",
            "+977-980-0000000",
            "  +9779800000000  ",
            "+977 (980) 0000000",
        ],
    )
    def test_equivalent_spellings_collapse_to_one_e164_value(self, raw: str) -> None:
        assert normalize_phone_number(raw) == "+9779800000000"

    @override_settings(DEFAULT_COUNTRY_CODE="NP")
    def test_local_format_uses_the_default_region(self) -> None:
        assert normalize_phone_number("9800000000") == "+9779800000000"

    @override_settings(DEFAULT_COUNTRY_CODE="GB")
    def test_default_region_is_configurable(self) -> None:
        assert normalize_phone_number("07911123456") == "+447911123456"

    def test_explicit_region_overrides_the_setting(self) -> None:
        assert normalize_phone_number("07911123456", default_region="GB") == "+447911123456"

    def test_leading_plus_ignores_the_default_region(self) -> None:
        with override_settings(DEFAULT_COUNTRY_CODE="GB"):
            assert normalize_phone_number("+9779800000000") == "+9779800000000"


class TestParsing:
    def test_returns_structured_parts(self) -> None:
        parsed = parse_phone_number("+9779800000000")
        assert parsed.e164 == "+9779800000000"
        assert parsed.country_code == "977"
        assert parsed.national_number == "9800000000"
        assert parsed.region == "NP"


class TestInvalidInput:
    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "not a number", "12", "+1", "abcdefghij", "+999999999999999999"],
    )
    def test_invalid_values_raise(self, raw: str) -> None:
        with pytest.raises(PhoneNumberError):
            normalize_phone_number(raw)

    def test_none_raises(self) -> None:
        with pytest.raises(PhoneNumberError):
            normalize_phone_number(None)  # type: ignore[arg-type]

    def test_error_message_mentions_the_offending_value(self) -> None:
        with pytest.raises(PhoneNumberError) as exc_info:
            normalize_phone_number("12345")
        assert "12345" in str(exc_info.value)

    def test_is_valid_phone_number_does_not_raise(self) -> None:
        assert is_valid_phone_number("+9779800000000") is True
        assert is_valid_phone_number("nonsense") is False


class TestDjangoValidator:
    def test_valid_number_passes(self) -> None:
        validate_phone_number("+9779800000000")

    def test_invalid_number_raises_django_validation_error(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_phone_number("nope")
        assert exc_info.value.code == "invalid_phone_number"
