"""Template context available on every page."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest


def site_context(request: HttpRequest) -> dict[str, object]:
    """
    Branding and environment banner data.

    ``is_mock_provider`` drives a prominent banner so nobody can mistake a
    simulated send for a real one, and no credential is ever exposed here.
    """
    provider = getattr(settings, "WHATSAPP_PROVIDER", "mock")
    return {
        # Deliberately not "site_name": django.contrib.auth's views inject
        # their own site_name into the context, which would shadow ours and
        # render the bare host name on the sign-in page.
        "brand_name": settings.SITE_NAME,
        "brand_tagline": settings.SITE_TAGLINE,
        "brand_description": settings.SITE_DESCRIPTION,
        "business_display_name": settings.BUSINESS_DISPLAY_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "whatsapp_provider": provider,
        "is_mock_provider": provider == "mock",
        "debug": settings.DEBUG,
    }
