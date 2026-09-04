"""
The SMS provider and its segment arithmetic.

Segments are the part worth testing hard. A gateway bills per segment, not per
message, and the boundary is not where anybody expects: 160 characters, unless
one character forces UCS-2, at which point it is 70. A customer who writes 155
characters and adds a single emoji goes from one segment to three and finds out
on an invoice.
"""

from __future__ import annotations

import pytest

from sms.providers.base import segment_count
from sms.providers.factory import get_provider
from sms.providers.mock_provider import MockSmsProvider

pytestmark = pytest.mark.django_db


class TestSegmentCounting:
    def test_an_empty_body_is_no_segments(self) -> None:
        assert segment_count("") == 0

    def test_a_short_message_is_one(self) -> None:
        assert segment_count("Your order is ready.") == 1

    def test_exactly_160_gsm7_characters_is_one(self) -> None:
        assert segment_count("a" * 160) == 1

    def test_161_is_two(self) -> None:
        """And each is 153, not 160: concatenation costs seven characters."""
        assert segment_count("a" * 161) == 2
        assert segment_count("a" * 306) == 2
        assert segment_count("a" * 307) == 3

    def test_one_emoji_more_than_halves_the_limit(self) -> None:
        """
        The expensive surprise. A body that fits in one segment as plain text
        becomes three the moment it stops being GSM-7.
        """
        plain = "a" * 155
        assert segment_count(plain) == 1
        assert segment_count(plain + "\U0001f600") == 3

    def test_accented_latin_is_still_gsm7(self) -> None:
        """é is in the GSM 03.38 alphabet, so it costs one position, not two."""
        assert segment_count("é" * 160) == 1
        assert segment_count("é" * 161) == 2

    def test_ucs2_boundaries(self) -> None:
        """A character outside GSM-7 drops the whole message to 70 per segment."""
        assert segment_count("你" * 70) == 1
        assert segment_count("你" * 71) == 2
        assert segment_count("你" * 134) == 2  # 67 each once concatenated
        assert segment_count("你" * 135) == 3

    def test_extended_gsm7_characters_cost_two(self) -> None:
        """A brace or a euro sign takes two positions, not one."""
        assert segment_count("{" * 80) == 1
        assert segment_count("{" * 81) == 2


class TestTheMockGateway:
    def test_a_valid_send_succeeds(self) -> None:
        result = MockSmsProvider().send_text(to="+9779800000001", body="Hello")

        assert result.success
        assert result.provider_message_id.startswith("mock_sms_")
        assert result.segments == 1

    def test_an_invalid_number_fails_permanently(self) -> None:
        """A number that is not a number will not become one on a retry."""
        result = MockSmsProvider().send_text(to="not-a-number", body="Hello")

        assert not result.success
        assert result.retryable is False
        assert result.error_code == "invalid_number"

    def test_an_empty_body_is_refused(self) -> None:
        result = MockSmsProvider().send_text(to="+9779800000001", body="   ")

        assert not result.success
        assert result.error_code == "empty_body"

    def test_it_reports_the_segments_it_would_bill(self) -> None:
        result = MockSmsProvider().send_text(to="+9779800000001", body="a" * 200)

        assert result.segments == 2

    def test_failures_can_be_simulated(self, settings) -> None:
        """A retry path that has never run is a retry path that does not work."""
        settings.MOCK_SMS_FAILURE_RATE = 1.0

        result = MockSmsProvider().send_text(to="+9779800000001", body="Hello")

        assert not result.success
        assert result.error_code.startswith("mock_")

    def test_it_does_not_log_the_full_number(self, caplog) -> None:
        """A phone number in a log file is personal data in a log file."""
        with caplog.at_level("INFO", logger="sms.providers.mock_provider"):
            MockSmsProvider().send_text(to="+9779812345678", body="Hi")

        assert "+9779812345678" not in caplog.text
        assert "5678" in caplog.text


class TestTheFactory:
    def test_it_builds_the_configured_provider(self, settings) -> None:
        settings.SMS_PROVIDER = "mock"

        assert isinstance(get_provider(), MockSmsProvider)

    def test_an_unknown_provider_is_refused_by_name(self, settings) -> None:
        from core.exceptions import ProviderNotConfigured

        settings.SMS_PROVIDER = "carrier-pigeon"

        with pytest.raises(ProviderNotConfigured, match="carrier-pigeon"):
            get_provider()

    def test_it_is_not_cached(self, settings) -> None:
        """Matching the other two factories, and for the same reasons."""
        assert get_provider() is not get_provider()


class TestTheInterfaceIsDeliberatelySmaller:
    def test_sms_has_no_template_methods(self) -> None:
        """
        The finding that shaped this stage. WhatsApp's contract is built around
        Meta's approval registry; SMS has no such thing, and making it implement
        that interface would have meant three methods raising NotImplementedError.
        """
        provider = MockSmsProvider()

        assert not hasattr(provider, "send_template")
        assert not hasattr(provider, "fetch_templates")

    def test_both_results_answer_the_same_questions(self) -> None:
        """
        What the two providers share is not an interface but a result shape, and
        that is what lets one Celery task drive either.
        """
        from whatsapp.services.base import SendResult

        sms = MockSmsProvider().send_text(to="+9779800000001", body="Hi")
        whatsapp = SendResult.ok("wamid.1")

        for attribute in ("success", "provider_message_id", "error_code", "retryable"):
            assert hasattr(sms, attribute)
            assert hasattr(whatsapp, attribute)
