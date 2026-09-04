"""
Logging filter that keeps credentials out of the log stream.

Even careful code can end up logging a request body or an exception repr that
embeds an access token. This filter is the last line of defence: it redacts
configured secret values and anything that structurally looks like a bearer
token or Meta access token.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

# Settings whose *values* must never appear in a log line.
#
# Kept honest by ``core.tests.test_logging_redaction``, which scans the settings
# module for anything that looks like a secret and fails if it is not listed
# here. The two Stage 4/5 entries below were missing for exactly as long as that
# test did not exist, which is the argument for the test.
SECRET_SETTING_NAMES = (
    "SECRET_KEY",
    "META_ACCESS_TOKEN",
    "META_APP_SECRET",
    "META_WEBHOOK_VERIFY_TOKEN",
    "EMAIL_HOST_PASSWORD",
    # The key every stored provider credential is encrypted with. Leaking this
    # into a log turns a database dump from useless into complete.
    "FIELD_ENCRYPTION_KEY",
    # Signs payment webhooks. With it, a stranger can forge a settled invoice.
    "PAYMENT_WEBHOOK_SECRET",
    # Previous signing keys, kept live during a rotation. Every bit as sensitive
    # as SECRET_KEY itself for as long as they are listed.
    "SECRET_KEY_FALLBACKS",
)

REDACTION = "[REDACTED]"

# Structural patterns, applied even when the exact secret is unknown.
PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{12,}=*"),
    re.compile(r"(?i)(\"?(?:access_token|app_secret|client_secret|password|api_key|secret_key|encryption_key|webhook_secret)\"?\s*[:=]\s*\"?)[^\s\",;}]+"),
    # Fernet ciphertext. Not a secret in itself, but a stored credential landing
    # in a log line means something is logging a model it should not be.
    re.compile(r"gAAAAA[A-Za-z0-9_\-=]{20,}"),
    re.compile(r"\bEA[A-Za-z0-9]{20,}\b"),  # Meta access tokens
)


class RedactSecretsFilter(logging.Filter):
    """Redacts secrets from the formatted message and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True

        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


#: Below this length a "secret" is almost certainly a placeholder, and redacting
#: a four-character string would blank out unrelated words all over the log.
MINIMUM_SECRET_LENGTH = 8


def _known_secrets() -> list[str]:
    """
    The secret *values* currently configured.

    Handles list-valued settings as well as strings: ``SECRET_KEY_FALLBACKS``
    holds previous signing keys during a rotation, and a name on the list whose
    values were silently skipped would be worse than not listing it — it
    would look protected.
    """
    values: list[str] = []

    for name in SECRET_SETTING_NAMES:
        value = getattr(settings, name, "")
        candidates = value if isinstance(value, list | tuple) else [value]
        values.extend(
            item
            for item in candidates
            if isinstance(item, str) and len(item) >= MINIMUM_SECRET_LENGTH
        )

    return values


def redact(text: str) -> str:
    """Return ``text`` with known and structurally-detected secrets removed."""
    if not text:
        return text

    for secret in _known_secrets():
        if secret in text:
            text = text.replace(secret, REDACTION)

    for pattern in PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)} {REDACTION}" if m.lastindex else REDACTION, text)

    return text
