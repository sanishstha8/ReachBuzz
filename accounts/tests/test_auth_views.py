"""Security tests for the HTML authentication flow."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


class TestLoginPage:
    def test_renders_for_anonymous_visitors(self, client: Client) -> None:
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200
        assert "Sign in" in response.content.decode()

    def test_contains_a_csrf_token(self, client: Client) -> None:
        response = client.get(reverse("accounts:login"))
        assert "csrfmiddlewaretoken" in response.content.decode()

    def test_valid_credentials_sign_the_user_in(self, client: Client, operator, password) -> None:
        response = client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": password},
            follow=True,
        )
        assert response.status_code == 200
        assert response.wsgi_request.user.is_authenticated

    def test_wrong_password_is_rejected(self, client: Client, operator) -> None:
        response = client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": "wrong-password"},
        )
        assert response.status_code == 200
        assert not response.wsgi_request.user.is_authenticated
        assert "Incorrect email address or password." in response.content.decode()

    def test_unknown_email_gives_the_same_message(self, client: Client) -> None:
        """Identical wording for both failures prevents account enumeration."""
        response = client.post(
            reverse("accounts:login"),
            {"username": "nobody@example.com", "password": "whatever-password"},
        )
        assert "Incorrect email address or password." in response.content.decode()

    def test_deactivated_account_cannot_sign_in(self, client: Client, operator, password) -> None:
        operator.is_active = False
        operator.save(update_fields=["is_active"])

        response = client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": password},
        )
        assert not response.wsgi_request.user.is_authenticated

    def test_session_key_is_rotated_on_login(self, client: Client, operator, password) -> None:
        client.get(reverse("accounts:login"))
        before = client.session.session_key

        client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": password},
        )
        assert client.session.session_key != before

    def test_next_parameter_is_honoured(self, client: Client, operator, password) -> None:
        target = reverse("accounts:profile")
        response = client.post(
            f"{reverse('accounts:login')}?next={target}",
            {"username": operator.email, "password": password, "next": target},
        )
        assert response.status_code == 302
        assert response.url == target


class TestCsrfProtection:
    def test_login_without_a_csrf_token_is_rejected(self, operator, password) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": password},
        )
        assert response.status_code == 403

    def test_logout_without_a_csrf_token_is_rejected(self, operator) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(operator)
        response = csrf_client.post(reverse("accounts:logout"))
        assert response.status_code == 403


class TestLogout:
    def test_get_is_not_allowed(self, auth_client: Client) -> None:
        """A GET logout would let any image or link sign a user out."""
        response = auth_client.get(reverse("accounts:logout"))
        assert response.status_code == 405

    def test_post_ends_the_session(self, auth_client: Client) -> None:
        response = auth_client.post(reverse("accounts:logout"))
        assert response.status_code == 302
        assert not response.wsgi_request.user.is_authenticated


class TestProfile:
    def test_requires_authentication(self, client: Client) -> None:
        response = client.get(reverse("accounts:profile"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_operator_can_update_their_name(self, auth_client: Client, operator) -> None:
        response = auth_client.post(
            reverse("accounts:profile"),
            {"first_name": "Renamed", "last_name": "Person"},
            follow=True,
        )
        operator.refresh_from_db()
        assert response.status_code == 200
        assert operator.first_name == "Renamed"

    def test_password_change_keeps_the_session_alive(self, auth_client: Client, operator, password) -> None:
        response = auth_client.post(
            reverse("accounts:profile"),
            {
                "change_password": "1",
                "old_password": password,
                "new_password1": "an-even-better-password",
                "new_password2": "an-even-better-password",
            },
            follow=True,
        )
        operator.refresh_from_db()
        assert response.status_code == 200
        assert operator.check_password("an-even-better-password")
        assert response.wsgi_request.user.is_authenticated

    def test_wrong_old_password_is_rejected(self, auth_client: Client, operator, password) -> None:
        auth_client.post(
            reverse("accounts:profile"),
            {
                "change_password": "1",
                "old_password": "not-the-password",
                "new_password1": "an-even-better-password",
                "new_password2": "an-even-better-password",
            },
        )
        operator.refresh_from_db()
        assert operator.check_password(password)


class TestAuthAuditing:
    def test_successful_login_is_audited(self, client: Client, operator, password) -> None:
        client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": password},
        )
        assert AuditLog.objects.filter(action=AuditAction.LOGIN, user=operator).exists()

    def test_failed_login_is_audited_without_the_password(self, client: Client, operator) -> None:
        client.post(
            reverse("accounts:login"),
            {"username": operator.email, "password": "super-secret-attempt"},
        )
        entry = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
        assert entry.user is None
        assert entry.metadata["attempted_identifier"] == operator.email
        assert "super-secret-attempt" not in str(entry.metadata)

    def test_logout_is_audited(self, auth_client: Client, operator) -> None:
        auth_client.post(reverse("accounts:logout"))
        assert AuditLog.objects.filter(action=AuditAction.LOGOUT, user=operator).exists()
