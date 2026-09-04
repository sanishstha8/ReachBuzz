"""
The public landing page.

Two things are worth testing about a marketing page, and neither is its
wording. First, that it is genuinely public — it is the only unauthenticated
HTML in the project, so a stray login requirement would take the front door
down. Second, that it does not claim things the product cannot do: no dead
links, no credentials, and no invented price.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(client: Client) -> str:
    response = client.get(reverse("pages:landing"))
    assert response.status_code == 200
    return response.content.decode()


class TestItIsPublic:
    def test_an_anonymous_visitor_gets_the_page(self, client: Client) -> None:
        assert client.get(reverse("pages:landing")).status_code == 200

    def test_it_lives_at_the_root(self) -> None:
        assert reverse("pages:landing") == "/"

    def test_a_signed_in_operator_can_still_read_it(self, auth_client: Client) -> None:
        assert auth_client.get(reverse("pages:landing")).status_code == 200

    def test_the_dashboard_is_still_behind_a_login(self, client: Client) -> None:
        """Moving it out of the root must not have moved it out of the gate."""
        response = client.get(reverse("dashboard:home"))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_signing_in_still_lands_on_the_dashboard(self, operator, password) -> None:
        client = Client()
        response = client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": password},
            follow=True,
        )

        assert response.redirect_chain[-1][0] == reverse("dashboard:home")


class TestItSaysTrueThings:
    def test_it_uses_the_configured_brand_name(self, page: str, settings) -> None:
        assert settings.SITE_NAME in page

    def test_it_does_not_use_the_name_from_the_reference_design(self, page: str) -> None:
        """The mockup said BulkMsg; this product is not called that."""
        assert "BulkMsg" not in page

    def test_no_price_is_invented_when_none_is_configured(self, page: str) -> None:
        """
        A tier with no figure says so. Filling the layout with a plausible
        number would be the page telling its first lie.
        """
        from billing.models import Plan

        assert not any(plan.has_price for plan in Plan.objects.public())
        assert "Pricing on request" in page

    def test_a_configured_price_is_shown_instead(self, client: Client) -> None:
        """Setting a real figure on the plan is all it takes to publish one."""
        from billing.models import Plan

        Plan.objects.public().update(price=None, is_public=False)
        Plan.objects.create(
            name="Priced", slug="priced", price="4500.00", currency="NPR", summary="x"
        )

        body = client.get(reverse("pages:landing")).content.decode()

        assert "NPR 4,500.00" in body
        assert "Pricing on request" not in body

    def test_the_advertised_limits_are_the_enforced_ones(self, page: str) -> None:
        """
        The point of moving tiers into the database. While the copy was a tuple
        in pages/views.py, the page could promise a ceiling that nothing checked
        — and did, for two releases.
        """
        from billing.models import Plan

        starter = Plan.objects.get(slug="starter")

        assert starter.max_contacts == 1000
        assert "Up to 1,000 contacts" in page

    def test_the_pricing_block_is_omitted_without_a_catalogue(self, client: Client) -> None:
        """An install with no plans shows no pricing section rather than an empty one."""
        from billing.models import Plan

        Plan.objects.update(is_public=False)

        body = client.get(reverse("pages:landing")).content.decode()

        assert "Pricing on request" not in body
        assert 'id="pricing"' not in body

    def test_the_meta_billing_caveat_is_stated(self, page: str) -> None:
        """Meta bills conversations separately; a pricing page must not imply otherwise."""
        assert "billed to you by Meta" in page

    def test_the_dashboard_preview_is_labelled_as_sample_data(self, page: str) -> None:
        """The numbers in the mock are illustrative and the page says so."""
        assert "sample data" in page.lower()


class TestEveryLinkResolves:
    def test_no_link_points_at_a_missing_page(self, page: str) -> None:
        """
        The reference design linked to a blog, a help centre and a refund
        policy. None of those exist here, so none of them are linked.
        """
        from django.test import Client as PlainClient

        # Anchors only. A <link rel="stylesheet"> also has an href, and static
        # files are not served under the test settings, so including those
        # would fail on something that is not a link at all.
        hrefs = set(re.findall(r'<a [^>]*href="(/[^"#]*)"', page))
        client = PlainClient()

        broken = {
            href: client.get(href).status_code
            for href in hrefs
            if client.get(href).status_code == 404
        }
        assert not broken, f"landing page links to missing pages: {broken}"

    def test_the_expected_destinations_are_present(self, page: str) -> None:
        assert reverse("accounts:login") in page
        assert reverse("api-docs") in page

    def test_every_section_the_nav_points_at_exists(self, page: str) -> None:
        """A nav link to an anchor that is not on the page is a dead link too."""
        anchors = set(re.findall(r'<a href="#([a-z-]+)"', page))
        ids = set(re.findall(r'id="([a-z-]+)"', page))

        assert anchors, "expected in-page navigation"
        assert anchors <= ids, f"nav points at missing sections: {anchors - ids}"


class TestItLeaksNothing:
    def test_no_credential_is_rendered(self, client: Client, settings) -> None:
        settings.META_ACCESS_TOKEN = "EAAtopsecrettoken1234567890"
        settings.META_APP_SECRET = "topsecretappsecret"
        settings.SECRET_KEY = "a-very-secret-key-value"

        body = client.get(reverse("pages:landing")).content.decode()

        assert "EAAtopsecrettoken1234567890" not in body
        assert "topsecretappsecret" not in body
        assert "a-very-secret-key-value" not in body

    def test_the_support_address_appears_only_when_configured(
        self, client: Client, settings
    ) -> None:
        settings.SUPPORT_EMAIL = "hello@example.com"
        assert "hello@example.com" in client.get(reverse("pages:landing")).content.decode()

        settings.SUPPORT_EMAIL = ""
        body = client.get(reverse("pages:landing")).content.decode()
        assert "mailto:" not in body


class TestContent:
    def test_every_section_from_the_design_is_present(self, page: str) -> None:
        for section in ("features", "how-it-works", "pricing", "faq", "contact"):
            assert f'id="{section}"' in page, section

    def test_all_features_render(self, page: str) -> None:
        from pages.views import FEATURES

        for feature in FEATURES:
            assert feature.title in page

    def test_all_steps_render_in_order(self, page: str) -> None:
        from pages.views import STEPS

        positions = [page.index(step.title) for step in STEPS]
        assert positions == sorted(positions), "steps rendered out of order"

    def test_the_faq_covers_consent(self, page: str) -> None:
        """The constraint the product is built around belongs on its front page."""
        assert "opted in" in page.lower()
