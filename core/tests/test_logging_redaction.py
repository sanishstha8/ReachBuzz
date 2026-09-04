"""
Credentials must not reach the log stream.

The filter itself has been tested since Phase 8. What was missing, and what this
file adds, is a guard on **the list it works from**: `FIELD_ENCRYPTION_KEY` and
`PAYMENT_WEBHOOK_SECRET` were added in Stages 4 and 5 and neither was registered
for redaction. Nobody noticed, because nothing was watching the list.

So the important test here is the last one. It scans the settings module for
anything that looks like a secret and fails if it is not covered, which turns
"remember to add it" into something the suite remembers instead.
"""

from __future__ import annotations

import logging

import pytest
from django.conf import settings

from core.logging_filters import SECRET_SETTING_NAMES, RedactSecretsFilter, redact

pytestmark = pytest.mark.django_db

#: Settings whose names look secret but whose values are not.
#:
#: Every entry needs a reason. "It is not really a secret" is a claim that has
#: to survive somebody reading this list later.
NOT_ACTUALLY_SECRET = {
    # A boolean, not a value.
    "WHATSAPP_REQUIRE_MESSAGING_ACCOUNT",
    # Names of secrets, not secrets.
    "SECRET_SETTING_NAMES",
    # A cache-key prefix. Public by nature; it appears in cache keys.
    "CACHE_MIDDLEWARE_KEY_PREFIX",
    # A filesystem path to a key, not the key. Redacting the path would make
    # a TLS misconfiguration much harder to diagnose from the logs.
    "EMAIL_SSL_KEYFILE",
    # A number of seconds.
    "PASSWORD_RESET_TIMEOUT",
}

SECRET_ISH = ("SECRET", "TOKEN", "PASSWORD", "KEY", "CREDENTIAL")


def looks_secret(name: str) -> bool:
    if not name.isupper() or name in NOT_ACTUALLY_SECRET:
        return False
    # PUBLIC_KEY-style names and Django internals that hold no secret value.
    if name in {"DEFAULT_AUTO_FIELD", "PASSWORD_HASHERS", "AUTH_PASSWORD_VALIDATORS"}:
        return False
    return any(word in name for word in SECRET_ISH)


class TestItRedactsWhatItKnows:
    def test_a_configured_secret_is_removed(self, settings) -> None:
        settings.META_ACCESS_TOKEN = "EAAsupersecrettoken1234567890"

        assert "EAAsupersecrettoken1234567890" not in redact(
            "sending with EAAsupersecrettoken1234567890"
        )

    def test_the_encryption_key_is_removed(self, settings) -> None:
        """
        Stage 5's key. Leaking this into a log turns a database dump from
        useless into complete, because every stored provider token is under it.
        """
        settings.FIELD_ENCRYPTION_KEY = "8Xn2sVQF0pKcJhWm4tRzYbL7dEgA1uNiO3vC6qMxPkU="

        assert "8Xn2sVQF0pKcJhWm4tRzYbL7dEgA1uNiO3vC6qMxPkU=" not in redact(
            "key=8Xn2sVQF0pKcJhWm4tRzYbL7dEgA1uNiO3vC6qMxPkU="
        )

    def test_the_webhook_secret_is_removed(self, settings) -> None:
        """With it, a stranger can forge a settled invoice."""
        settings.PAYMENT_WEBHOOK_SECRET = "whsec-abcdef1234567890"

        assert "whsec-abcdef1234567890" not in redact("signing with whsec-abcdef1234567890")

    def test_stored_ciphertext_is_removed(self) -> None:
        """
        Not a secret in itself. But a Fernet blob in a log line means something
        is logging a model it should not be, and the blob is the evidence.
        """
        blob = "gAAAAABmZ0FakeCiphertextValueThatIsLongEnough123456"

        assert blob not in redact(f"account token={blob}")

    def test_a_bearer_header_is_removed_without_knowing_the_value(self) -> None:
        assert "abcdefghijklmnop" not in redact("Authorization: Bearer abcdefghijklmnop")

    def test_ordinary_text_survives(self) -> None:
        """A filter that mangles every log line is one somebody switches off."""
        message = "Campaign 41 completed: 900 delivered, 3 failed"

        assert redact(message) == message


class TestTheFilterIsWiredIn:
    def test_it_redacts_a_real_log_record(self, settings) -> None:
        settings.META_ACCESS_TOKEN = "EAAanotherverysecretvalue00"
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="token is %s", args=("EAAanotherverysecretvalue00",), exc_info=None,
        )

        RedactSecretsFilter().filter(record)

        assert "EAAanotherverysecretvalue00" not in record.getMessage()

    def test_it_is_installed_in_the_logging_config(self) -> None:
        filters = settings.LOGGING.get("filters", {})

        assert any(
            "RedactSecretsFilter" in str(value.get("()", "")) for value in filters.values()
        ), "the filter exists but nothing is using it"


class TestNothingSecretIsUnlisted:
    """
    The test that would have caught the two-stage gap.

    Scanning the settings rather than maintaining a second hand-written list:
    a duplicate list has the same failure mode as the first one.
    """

    def test_every_secret_looking_setting_is_registered(self) -> None:
        unlisted = sorted(
            name
            for name in dir(settings)
            if looks_secret(name) and name not in SECRET_SETTING_NAMES
        )

        assert not unlisted, (
            "these settings look like credentials but their values would appear "
            f"in a log line unredacted: {unlisted}. Add them to "
            "core.logging_filters.SECRET_SETTING_NAMES, or to NOT_ACTUALLY_SECRET "
            "in this file with a reason."
        )

    def test_the_two_that_were_missing_are_covered(self) -> None:
        """Named explicitly, so a careless edit to the scan cannot hide them."""
        assert "FIELD_ENCRYPTION_KEY" in SECRET_SETTING_NAMES
        assert "PAYMENT_WEBHOOK_SECRET" in SECRET_SETTING_NAMES
