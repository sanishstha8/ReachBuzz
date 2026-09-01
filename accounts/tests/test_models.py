"""User model, manager and the role capability matrix."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from accounts.models import UserRole

User = get_user_model()

pytestmark = pytest.mark.django_db


class TestUserManager:
    def test_create_user_normalizes_and_lowercases_the_email(self) -> None:
        user = User.objects.create_user(email="Operator@Example.COM", password="a-good-password")
        assert user.email == "operator@example.com"

    def test_create_user_hashes_the_password(self) -> None:
        user = User.objects.create_user(email="a@example.com", password="a-good-password")
        assert user.password != "a-good-password"
        assert user.check_password("a-good-password")

    def test_email_is_required(self) -> None:
        with pytest.raises(ValueError):
            User.objects.create_user(email="", password="a-good-password")

    def test_email_is_unique(self) -> None:
        from django.db import IntegrityError

        User.objects.create_user(email="dup@example.com", password="a-good-password")
        with pytest.raises(IntegrityError):
            User.objects.create_user(email="dup@example.com", password="a-good-password")

    def test_new_users_default_to_operator(self) -> None:
        user = User.objects.create_user(email="b@example.com", password="a-good-password")
        assert user.role == UserRole.OPERATOR
        assert user.is_staff is False

    def test_create_superuser_is_an_administrator(self) -> None:
        user = User.objects.create_superuser(email="root@example.com", password="a-good-password")
        assert user.is_superuser and user.is_staff
        assert user.role == UserRole.ADMINISTRATOR

    def test_create_superuser_rejects_contradictory_flags(self) -> None:
        with pytest.raises(ValueError):
            User.objects.create_superuser(email="x@example.com", password="pw", is_staff=False)


class TestCapabilities:
    def test_operator_can_manage_and_launch(self, operator) -> None:
        assert operator.can_manage_contacts
        assert operator.can_manage_campaigns
        assert operator.can_launch_campaigns
        assert not operator.is_administrator

    def test_viewer_is_read_only(self, viewer) -> None:
        assert not viewer.can_manage_contacts
        assert not viewer.can_manage_campaigns
        assert not viewer.can_launch_campaigns
        assert not viewer.is_administrator

    def test_administrator_has_every_capability(self, administrator) -> None:
        assert administrator.is_administrator
        assert administrator.can_manage_contacts
        assert administrator.can_manage_campaigns
        assert administrator.can_launch_campaigns

    def test_superuser_is_treated_as_administrator_regardless_of_role(self, superuser) -> None:
        superuser.role = UserRole.VIEWER
        assert superuser.is_administrator


class TestDisplay:
    def test_display_name_prefers_the_full_name(self, operator) -> None:
        assert operator.display_name == "Test User"

    def test_display_name_falls_back_to_the_email(self, db) -> None:
        user = User.objects.create_user(email="noname@example.com", password="a-good-password")
        assert user.display_name == "noname@example.com"

    def test_initials(self, operator) -> None:
        assert operator.initials == "TU"

    def test_str_is_the_email(self, operator) -> None:
        assert str(operator) == "operator@example.com"
