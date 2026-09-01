"""
Authentication auditing.

Sign-in activity is part of the compliance trail: it answers who had access to
the contact list and campaign controls at a given time.
"""

from __future__ import annotations

import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from core.audit import client_ip, record_audit
from core.models import AuditAction

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs) -> None:
    ip = client_ip(request)
    if ip != user.last_login_ip:
        user.last_login_ip = ip
        user.save(update_fields=["last_login_ip"])
    record_audit(AuditAction.LOGIN, user=user, request=request, description="Signed in")


@receiver(user_logged_out)
def on_user_logged_out(sender, request, user, **kwargs) -> None:
    if user is not None:
        record_audit(AuditAction.LOGOUT, user=user, request=request, description="Signed out")


@receiver(user_login_failed)
def on_user_login_failed(sender, credentials, request=None, **kwargs) -> None:
    # Only the attempted identifier is recorded. The submitted password is
    # never logged, stored, or echoed anywhere.
    attempted = credentials.get("username") or credentials.get("email") or ""
    record_audit(
        AuditAction.LOGIN_FAILED,
        user=None,
        request=request,
        description="Failed sign-in attempt",
        metadata={"attempted_identifier": attempted[:150]},
    )
    logger.warning("Failed login attempt for %s from %s", attempted[:150], client_ip(request))
