"""Authentication REST API."""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


class TestLoginEndpoint:
    def test_valid_credentials_return_the_user(self, api_client: APIClient, operator, password) -> None:
        response = api_client.post(
            reverse("accounts-api:login"),
            {"email": operator.email, "password": password},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["email"] == operator.email
        assert response.data["capabilities"]["can_launch_campaigns"] is True

    def test_response_never_contains_the_password(self, api_client: APIClient, operator, password) -> None:
        response = api_client.post(
            reverse("accounts-api:login"),
            {"email": operator.email, "password": password},
            format="json",
        )
        assert "password" not in response.data

    def test_invalid_credentials_return_400_with_the_error_envelope(
        self, api_client: APIClient, operator
    ) -> None:
        response = api_client.post(
            reverse("accounts-api:login"),
            {"email": operator.email, "password": "wrong-password"},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["code"] == "validation_failed"
        assert "errors" in response.data

    def test_deactivated_account_cannot_authenticate(
        self, api_client: APIClient, operator, password
    ) -> None:
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        response = api_client.post(
            reverse("accounts-api:login"),
            {"email": operator.email, "password": password},
            format="json",
        )
        assert response.status_code == 400

    def test_email_is_case_insensitive(self, api_client: APIClient, operator, password) -> None:
        response = api_client.post(
            reverse("accounts-api:login"),
            {"email": operator.email.upper(), "password": password},
            format="json",
        )
        assert response.status_code == 200


class TestCurrentUserEndpoint:
    def test_requires_authentication(self, api_client: APIClient) -> None:
        response = api_client.get(reverse("accounts-api:me"))
        assert response.status_code in (401, 403)

    def test_returns_the_signed_in_user(self, auth_api_client: APIClient, operator) -> None:
        response = auth_api_client.get(reverse("accounts-api:me"))
        assert response.status_code == 200
        assert response.data["id"] == str(operator.id)
        assert response.data["role"] == operator.role

    def test_viewer_capabilities_are_reported_accurately(self, viewer) -> None:
        client = APIClient()
        client.force_login(viewer)

        response = client.get(reverse("accounts-api:me"))

        assert response.data["capabilities"] == {
            "is_administrator": False,
            "can_manage_contacts": False,
            "can_manage_campaigns": False,
            "can_launch_campaigns": False,
        }


class TestLogoutEndpoint:
    def test_requires_authentication(self, api_client: APIClient) -> None:
        response = api_client.post(reverse("accounts-api:logout"))
        assert response.status_code in (401, 403)

    def test_ends_the_session(self, auth_api_client: APIClient) -> None:
        assert auth_api_client.post(reverse("accounts-api:logout")).status_code == 204
        assert auth_api_client.get(reverse("accounts-api:me")).status_code in (401, 403)


class TestApiSchema:
    def test_schema_is_reachable_and_documents_the_auth_endpoints(
        self, auth_api_client: APIClient
    ) -> None:
        response = auth_api_client.get(reverse("api-schema"))
        assert response.status_code == 200
        assert b"/api/auth/login/" in response.content
