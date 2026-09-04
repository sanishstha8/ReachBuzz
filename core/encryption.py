"""
Encryption at rest for secrets that cannot live in the environment.

This project's standing rule is that credentials live in the environment and
nowhere else. Multi-tenancy breaks that rule's *mechanism* without changing its
intent: once every customer has their own WhatsApp access token, there is no
environment variable to put them in.

So the rule becomes: **the key lives in the environment, and the secrets live
encrypted under it.** A database dump on its own reveals nothing. Getting a
customer's token requires the database *and* the deployment's key, which is the
same bar as before.

What this is not:

* **Not a substitute for not storing something.** Anything that does not have to
  be stored still is not. This exists for the tokens that genuinely must be.
* **Not protection from the running application.** A process that can decrypt is
  a process that can read. It protects backups, replicas, and dumps — which is
  most of the realistic exposure.

Fernet (AES-128-CBC with an HMAC) rather than a hand-rolled construction: it is
authenticated, so a tampered ciphertext fails loudly instead of decrypting to
rubbish, and it is nonce-managed, so the same value encrypts differently every
time and an attacker cannot tell which two customers share a token.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


class DecryptionFailed(Exception):
    """
    A stored secret could not be read back.

    Almost always one of two things: the key changed, or the row is corrupt.
    Both need a human, and neither should be papered over by returning an empty
    string — an empty token would be sent to a provider as if it were real.
    """


def _key() -> bytes:
    """
    The deployment's encryption key, as Fernet wants it.

    Falls back to deriving one from ``SECRET_KEY`` when ``FIELD_ENCRYPTION_KEY``
    is unset. That fallback is a convenience for development and a hazard in
    production, which is why ``check --deploy`` warns about it: rotating
    ``SECRET_KEY`` would then silently make every stored token unreadable.
    """
    configured = getattr(settings, "FIELD_ENCRYPTION_KEY", "")
    if configured:
        try:
            Fernet(configured.encode() if isinstance(configured, str) else configured)
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"'
            ) from exc
        return configured.encode() if isinstance(configured, str) else configured

    secret = getattr(settings, "SECRET_KEY", "")
    if not secret:
        raise ImproperlyConfigured("Neither FIELD_ENCRYPTION_KEY nor SECRET_KEY is set.")

    # SHA-256 of SECRET_KEY, urlsafe-base64'd: a deterministic 32-byte key that
    # is at least uniformly distributed, unlike SECRET_KEY's own bytes.
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(value: str) -> str:
    """Encrypt a string for storage. An empty value stays empty, not encrypted."""
    if not value:
        return ""
    return Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """
    Read a stored secret back.

    Raises rather than returning "" on failure. An empty token handed to a
    provider looks like a configuration mistake at the far end, days later; an
    exception here says what actually happened, now.
    """
    if not value:
        return ""
    try:
        return Fernet(_key()).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        # Deliberately says nothing about the value itself.
        raise DecryptionFailed(
            "A stored credential could not be decrypted. The encryption key has "
            "probably changed since it was saved."
        ) from exc


def is_encrypted(value: str) -> bool:
    """Whether a stored value can be read with the current key. Never raises."""
    try:
        decrypt(value)
    except DecryptionFailed:
        return False
    return True


def generate_key() -> str:
    """A fresh key, for the setup instructions and the management command."""
    return Fernet.generate_key().decode()
