"""
Deployment checks this project adds to Django's own.

Both of these are things that are fine in development and quietly harmful in
production, which is exactly the gap ``check --deploy`` exists to close.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning, register


@register(deploy=True)
def encryption_key_is_set(app_configs, **kwargs):
    """
    Warn when per-organization credentials are encrypted with a derived key.

    Without ``FIELD_ENCRYPTION_KEY``, the key comes from ``SECRET_KEY``. That
    works, but it silently couples two unrelated rotations together: changing
    ``SECRET_KEY`` — which is otherwise a routine, safe thing to do — would make
    every stored provider credential permanently unreadable.
    """
    if getattr(settings, "FIELD_ENCRYPTION_KEY", ""):
        return []

    return [
        Warning(
            "FIELD_ENCRYPTION_KEY is not set, so stored provider credentials are "
            "encrypted with a key derived from SECRET_KEY.",
            hint=(
                "Generate one with `python manage.py generate_encryption_key` and set "
                "FIELD_ENCRYPTION_KEY. Otherwise rotating SECRET_KEY makes every stored "
                "WhatsApp access token unreadable, with no recovery but re-entering them."
            ),
            id="core.W001",
        )
    ]


@register(deploy=True)
def payments_are_not_randomly_failing(app_configs, **kwargs):
    """
    Warn when the mock provider is set to fake declines outside development.

    There is deliberately no warning for ``PAYMENT_PROVIDER=mock`` itself. It is
    the only implementation there is, so such a check would be red on every run
    of every deployment forever — and a check that is always red is a check
    everybody learns to scroll past, which costs more than it saves. The README
    says plainly that no gateway is integrated.
    """
    if float(getattr(settings, "MOCK_PAYMENT_FAILURE_RATE", 0.0) or 0.0) <= 0:
        return []

    return [
        Warning(
            "MOCK_PAYMENT_FAILURE_RATE is above zero, so charges fail at random.",
            hint="Set it to 0.0 outside development.",
            id="core.W002",
        )
    ]
