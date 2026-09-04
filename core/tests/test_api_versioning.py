"""
The API is at two addresses and means one thing.

`/api/v1/` is canonical. The bare `/api/` is where the integrations that predate
versioning are pointing, and a version scheme is not a reason to break them.

The properties worth pinning are that both work, that they behave identically,
that everything this application generates points at the versioned form, and
that the reference documents one of them rather than listing every endpoint
twice.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

#: One route from each app, named rather than written out.
ROUTES = [
    "contacts-api:contact-list",
    "whatsapp-api:template-list",
    "campaigns-api:campaign-list",
    "dashboard-api:report-overview",
]


class TestBothPrefixesWork:
    @pytest.mark.parametrize("name", ROUTES)
    def test_the_versioned_route_answers(self, auth_api_client: APIClient, name: str) -> None:
        assert auth_api_client.get(reverse(name)).status_code == 200

    @pytest.mark.parametrize("name", ROUTES)
    def test_the_unversioned_route_answers_too(
        self, auth_api_client: APIClient, name: str
    ) -> None:
        """An existing client that never learns about v1 keeps working."""
        legacy = reverse(name).replace("/api/v1/", "/api/", 1)

        assert auth_api_client.get(legacy).status_code == 200

    def test_they_return_the_same_thing(self, auth_api_client: APIClient, make_contact) -> None:
        """
        Not two APIs that happen to look alike — the same urlconf, mounted
        twice. If these ever diverge, one of them is a fork nobody meant to make.
        """
        make_contact("Somebody", opted_in=True)
        versioned = reverse("contacts-api:contact-list")
        legacy = versioned.replace("/api/v1/", "/api/", 1)

        assert (
            auth_api_client.get(versioned).json()
            == auth_api_client.get(legacy).json()
        )

    def test_authentication_is_required_on_both(self, api_client: APIClient) -> None:
        """The alias is not a way around anything."""
        versioned = reverse("contacts-api:contact-list")
        legacy = versioned.replace("/api/v1/", "/api/", 1)

        assert api_client.get(versioned).status_code in (401, 403)
        assert api_client.get(legacy).status_code in (401, 403)

    def test_tenant_scoping_holds_on_both(
        self, auth_api_client: APIClient, other_organization, make_contact
    ) -> None:
        """The alias is not a way around that either."""
        make_contact("Theirs", "+9779800000077", organization=other_organization)
        versioned = reverse("contacts-api:contact-list")
        legacy = versioned.replace("/api/v1/", "/api/", 1)

        for url in (versioned, legacy):
            assert auth_api_client.get(url).json()["count"] == 0, url


class TestTheApplicationPointsAtV1:
    @pytest.mark.parametrize("name", ROUTES)
    def test_reverse_produces_the_versioned_path(self, name: str) -> None:
        """
        The canonical namespaces are registered on v1, so anything that builds a
        URL — a serializer's hyperlink, a redirect, a docs example — emits the
        versioned form without having to be told.
        """
        assert reverse(name).startswith("/api/v1/")

    def test_the_unversioned_include_has_its_own_namespace(self) -> None:
        """
        Otherwise which of the two `reverse()` returns is left to registration
        order, and it must always be v1.
        """
        assert reverse("contacts-api-unversioned:contact-list").startswith("/api/")
        assert not reverse("contacts-api-unversioned:contact-list").startswith("/api/v1/")


class TestTheSchemaDocumentsOneOfThem:
    def test_every_documented_path_is_versioned(self, auth_api_client: APIClient) -> None:
        """
        Two identical entries per endpoint helps nobody choose. The reference
        describes the address a new client should use.
        """
        schema = json.loads(auth_api_client.get(reverse("api-schema"), {"format": "json"}).content)

        unversioned = [
            path
            for path in schema["paths"]
            if not path.startswith("/api/v1/") and not path.startswith("/api/schema")
        ]

        assert unversioned == []

    def test_it_still_documents_the_whole_api(self, auth_api_client: APIClient) -> None:
        """Filtering the aliases must not have filtered anything real."""
        schema = json.loads(auth_api_client.get(reverse("api-schema"), {"format": "json"}).content)

        for name in ROUTES:
            assert reverse(name) in schema["paths"], name

    def test_the_version_is_declared(self, auth_api_client: APIClient) -> None:
        schema = json.loads(auth_api_client.get(reverse("api-schema"), {"format": "json"}).content)

        assert schema["info"]["version"] == "1.0.0"
