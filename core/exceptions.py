"""
Domain exceptions and the DRF exception handler.

Two rules drive this module:

1. Service layers raise *domain* exceptions that know nothing about HTTP.
2. Responses to clients are consistent and never leak credentials, stack
   traces or provider internals. Full detail goes to the log instead.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base class for expected, business-rule failures."""

    default_message = "The request could not be completed."
    status_code = status.HTTP_400_BAD_REQUEST
    code = "domain_error"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class ValidationFailed(DomainError):
    default_message = "The submitted data is not valid."
    status_code = status.HTTP_400_BAD_REQUEST
    code = "validation_failed"


class InvalidStateTransition(DomainError):
    """Raised when a campaign or message is asked to move to an illegal state."""

    default_message = "This action is not allowed in the current state."
    status_code = status.HTTP_409_CONFLICT
    code = "invalid_state_transition"


class ConflictError(DomainError):
    default_message = "The resource conflicts with existing data."
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class NotAllowed(DomainError):
    default_message = "You do not have permission to perform this action."
    status_code = status.HTTP_403_FORBIDDEN
    code = "not_allowed"


class ProviderError(DomainError):
    """
    A failure returned by (or while reaching) the WhatsApp provider.

    ``retryable`` tells the Celery task whether another attempt makes sense.
    ``provider_code`` is Meta's numeric error code when one was supplied.
    """

    default_message = "The messaging provider could not process this request."
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "provider_error"

    def __init__(
        self,
        message: str | None = None,
        *,
        provider_code: str = "",
        retryable: bool = False,
        retry_after: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, details=details)
        self.provider_code = provider_code
        self.retryable = retryable
        self.retry_after = retry_after


class ProviderRateLimited(ProviderError):
    default_message = "The messaging provider is rate limiting requests. Sending will resume shortly."
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "provider_rate_limited"

    def __init__(self, message: str | None = None, **kwargs: Any):
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class ProviderNotConfigured(DomainError):
    """Raised when the selected provider is missing required configuration."""

    default_message = (
        "The WhatsApp provider is not configured. Check the WHATSAPP_PROVIDER and "
        "META_* environment variables."
    )
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "provider_not_configured"


class WebhookVerificationError(DomainError):
    default_message = "Webhook signature verification failed."
    status_code = status.HTTP_403_FORBIDDEN
    code = "webhook_verification_failed"


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """
    DRF exception handler producing a consistent error envelope:

        {"detail": "...", "code": "...", "errors": {...}}

    Unexpected exceptions are logged with a correlation id that is echoed to the
    client, so an operator can find the traceback without the response ever
    carrying one.
    """
    if isinstance(exc, DomainError):
        return Response(
            {"detail": exc.message, "code": exc.code, "errors": exc.details},
            status=exc.status_code,
        )

    if isinstance(exc, DjangoValidationError):
        return Response(
            {
                "detail": "The submitted data is not valid.",
                "code": "validation_failed",
                "errors": exc.message_dict if hasattr(exc, "message_dict") else {"non_field_errors": exc.messages},
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    response = drf_exception_handler(exc, context)

    if response is not None:
        data = response.data
        if isinstance(data, dict) and "detail" in data and len(data) == 1:
            response.data = {
                "detail": str(data["detail"]),
                "code": getattr(data["detail"], "code", "error"),
                "errors": {},
            }
        elif isinstance(data, dict):
            response.data = {
                "detail": "The submitted data is not valid.",
                "code": "validation_failed",
                "errors": data,
            }
        return response

    if isinstance(exc, (Http404, PermissionDenied)):  # pragma: no cover - handled by DRF
        return None

    incident_id = uuid.uuid4().hex[:12]
    view = context.get("view")
    logger.exception(
        "Unhandled exception in %s (incident %s)",
        view.__class__.__name__ if view else "unknown view",
        incident_id,
    )
    return Response(
        {
            "detail": "An unexpected error occurred. Quote this reference when reporting it.",
            "code": "internal_error",
            "errors": {"incident_id": incident_id},
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
