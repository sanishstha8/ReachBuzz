"""
Branding context.

The variable is ``brand_name``, not ``site_name``, because
``django.contrib.auth``'s views inject their own ``site_name`` and would shadow
ours — which rendered the bare host name on the sign-in page.
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestBrandName:
    @override_settings(SITE_NAME="ReBuzz")
    def test_brand_appears_on_the_sign_in_page(self, client: Client) -> None:
        """Regression: Django's auth views shadow a `site_name` context key."""
        body = client.get(reverse("accounts:login")).content.decode()

        assert "ReBuzz" in body
        assert "127.0.0.1" not in body
        assert "testserver" not in body

    @override_settings(SITE_NAME="ReBuzz")
    def test_brand_appears_in_the_page_title(self, client: Client) -> None:
        body = client.get(reverse("accounts:login")).content.decode()
        assert "<title>Sign in &middot; ReBuzz</title>" in body

    @override_settings(SITE_NAME="ReBuzz")
    def test_brand_appears_on_an_authenticated_page(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("dashboard:home")).content.decode()
        assert "ReBuzz" in body

    @override_settings(SITE_NAME="Something Else")
    def test_the_brand_is_configurable(self, client: Client) -> None:
        body = client.get(reverse("accounts:login")).content.decode()
        assert "Something Else" in body

    def test_no_credential_reaches_the_template_context(self, auth_client: Client) -> None:
        from core.context_processors import site_context

        context = site_context(auth_client.get(reverse("dashboard:home")).wsgi_request)

        assert "META_ACCESS_TOKEN" not in context
        assert not any(
            isinstance(value, str) and value.startswith("EAA") for value in context.values()
        )
