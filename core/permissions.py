"""
Reusable DRF permission classes.

Authorization is role-based (see ``accounts.models.UserRole``). These classes
deliberately ask the user object for a capability rather than comparing role
strings, so the role matrix can change in one place.
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsActiveUser(BasePermission):
    """Authenticated *and* still enabled. Deactivating a user cuts API access."""

    message = "Your account is not active."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and user.is_active)


class _CapabilityPermission(BasePermission):
    """Grants read to any active user, writes only to users with ``capability``."""

    capability = ""
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        if request.method in SAFE_METHODS:
            return True
        return bool(getattr(user, self.capability, False))


class CanManageContacts(_CapabilityPermission):
    capability = "can_manage_contacts"
    message = "You do not have permission to modify contacts."


class CanManageCampaigns(_CapabilityPermission):
    capability = "can_manage_campaigns"
    message = "You do not have permission to modify campaigns."


class CanLaunchCampaigns(BasePermission):
    """Sending is the one irreversible action, so it is checked on its own."""

    message = "You do not have permission to send campaigns."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and getattr(user, "can_launch_campaigns", False)
        )


class IsAdministrator(BasePermission):
    message = "Administrator access is required."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and getattr(user, "is_administrator", False)
        )
