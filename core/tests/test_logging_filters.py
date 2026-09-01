"""Credentials must never reach the log stream."""

from __future__ import annotations

import logging

from django.test import override_settings

from core.logging_filters import REDACTION, RedactSecretsFilter, redact


def _record(message: str, *args) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, args, None)


class TestRedact:
    @override_settings(META_ACCESS_TOKEN="EAAsupersecretvalue1234567890")
    def test_known_setting_value_is_removed(self) -> None:
        result = redact("calling graph with token EAAsupersecretvalue1234567890 for phone 123")
        assert "EAAsupersecretvalue1234567890" not in result
        assert REDACTION in result
        # Non-secret context survives, so the log line is still useful.
        assert "phone 123" in result

    def test_bearer_header_is_removed(self) -> None:
        result = redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
        assert "abcdefghijklmnopqrstuvwxyz012345" not in result

    def test_json_secret_keys_are_removed(self) -> None:
        result = redact('{"access_token": "abc123def456ghi", "to": "+9779800000000"}')
        assert "abc123def456ghi" not in result
        assert "+9779800000000" in result

    def test_meta_token_shape_is_removed_even_when_unknown(self) -> None:
        result = redact("token=EAAGm0PX4ZCpsBO1234567890abcdefgh")
        assert "EAAGm0PX4ZCpsBO1234567890abcdefgh" not in result

    def test_plain_message_is_untouched(self) -> None:
        message = "sent message 7f3a to +9779800000000"
        assert redact(message) == message


class TestFilter:
    @override_settings(META_APP_SECRET="topsecretappsecret123")
    def test_filter_rewrites_the_record(self) -> None:
        record = _record("secret is %s", "topsecretappsecret123")
        assert RedactSecretsFilter().filter(record) is True
        assert "topsecretappsecret123" not in record.getMessage()

    def test_filter_always_allows_the_record_through(self) -> None:
        assert RedactSecretsFilter().filter(_record("hello")) is True
