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
SECRET_SETTING_NAMES = (
    "SECRET_KEY",
    "META_ACCESS_TOKEN",
    "META_APP_SECRET",
    "META_WEBHOOK_VERIFY_TOKEN",
    "EMAIL_HOST_PASSWORD",
)

REDACTION = "[REDACTED]"

# Structural patterns, applied even when the exact secret is unknown.
PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{12,}=*"),
    re.compile(r"(?i)(\"?(?:access_token|app_secret|client_secret|password|api_key|secret_key)\"?\s*[:=]\s*\"?)[^\s\",;}]+"),
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


def _known_secrets() -> list[str]:
    values = []
    for name in SECRET_SETTING_NAMES:
        value = getattr(settings, name, "")
        if isinstance(value, str) and len(value) >= 8:
            values.append(value)
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
