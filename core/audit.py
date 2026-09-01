"""Helper for writing audit trail entries."""

from __future__ import annotations

import logging
from typing import Any

from django.db import models
from django.http import HttpRequest

from core.models import AuditLog

logger = logging.getLogger(__name__)


def client_ip(request: HttpRequest | None) -> str | None:
    """Best-effort client IP, honouring a single proxy hop."""
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def record_audit(
    action: str,
    *,
    user=None,
    request: HttpRequest | None = None,
    obj: models.Model | None = None,
    description: str = "",
    metadata: dict[str, Any] | None = None,
) -> AuditLog | None:
    """
    Write an :class:`~core.models.AuditLog` row.

    Auditing must never break the operation it is recording, so failures are
    logged and swallowed.
    """
    if user is None and request is not None and getattr(request, "user", None):
        user = request.user if request.user.is_authenticated else None

    try:
        return AuditLog.objects.create(
            user=user,
            action=action,
            object_type=obj.__class__.__name__ if obj is not None else "",
            object_id=str(obj.pk) if obj is not None and obj.pk else "",
            description=description[:255],
            metadata=metadata or {},
            ip_address=client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:255] if request else ""),
        )
    except Exception:  # pragma: no cover - auditing must not break the request
        logger.exception("Failed to write audit log entry for action %s", action)
        return None
