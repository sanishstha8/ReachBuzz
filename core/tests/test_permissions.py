"""Authorization policy: capabilities, not role strings, decide access."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from rest_framework.test import APIRequestFactory

from core.permissions import (
    CanLaunchCampaigns,
    CanManageCampaigns,
    CanManageContacts,
    IsActiveUser,
    IsAdministrator,
)

pytestmark = pytest.mark.django_db

factory = APIRequestFactory()


def _request(method: str, user):
    request = getattr(factory, method)("/api/contacts/")
    request.user = user
    return request


class TestIsActiveUser:
    def test_anonymous_is_denied(self) -> None:
        assert not IsActiveUser().has_permission(_request("get", AnonymousUser()), None)

    def test_active_user_is_allowed(self, viewer) -> None:
        assert IsActiveUser().has_permission(_request("get", viewer), None)

    def test_deactivated_user_is_denied(self, operator) -> None:
        operator.is_active = False
        assert not IsActiveUser().has_permission(_request("get", operator), None)


class TestCapabilityPermissions:
    @pytest.mark.parametrize(
        "permission_class",
        [CanManageContacts, CanManageCampaigns],
    )
    def test_viewer_may_read_but_not_write(self, viewer, permission_class) -> None:
        permission = permission_class()
        assert permission.has_permission(_request("get", viewer), None)
        assert not permission.has_permission(_request("post", viewer), None)

    @pytest.mark.parametrize(
        "permission_class",
        [CanManageContacts, CanManageCampaigns],
    )
    def test_operator_may_write(self, operator, permission_class) -> None:
        permission = permission_class()
        assert permission.has_permission(_request("post", operator), None)
        assert permission.has_permission(_request("delete", operator), None)

    def test_anonymous_may_not_even_read(self, permission_class=CanManageContacts) -> None:
        assert not permission_class().has_permission(_request("get", AnonymousUser()), None)


class TestCanLaunchCampaigns:
    def test_viewer_cannot_launch_even_with_a_safe_method(self, viewer) -> None:
        """Sending is irreversible, so this permission has no read exemption."""
        assert not CanLaunchCampaigns().has_permission(_request("get", viewer), None)

    def test_operator_can_launch(self, operator) -> None:
        assert CanLaunchCampaigns().has_permission(_request("post", operator), None)

    def test_deactivated_operator_cannot_launch(self, operator) -> None:
        operator.is_active = False
        assert not CanLaunchCampaigns().has_permission(_request("post", operator), None)


class TestIsAdministrator:
    def test_operator_is_denied(self, operator) -> None:
        assert not IsAdministrator().has_permission(_request("get", operator), None)

    def test_administrator_is_allowed(self, administrator) -> None:
        assert IsAdministrator().has_permission(_request("get", administrator), None)

    def test_superuser_is_allowed(self, superuser) -> None:
        assert IsAdministrator().has_permission(_request("get", superuser), None)
