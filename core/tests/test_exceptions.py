"""The API error envelope must be consistent and must never leak internals."""

from __future__ import annotations

from unittest.mock import Mock

from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from core.exceptions import (
    ConflictError,
    InvalidStateTransition,
    ProviderError,
    ProviderRateLimited,
    api_exception_handler,
)


def _context() -> dict:
    view = Mock()
    view.__class__.__name__ = "DummyView"
    return {"view": view, "request": Mock()}


class TestDomainErrors:
    def test_invalid_state_transition_maps_to_409(self) -> None:
        response = api_exception_handler(
            InvalidStateTransition("A completed campaign cannot be launched again."), _context()
        )
        assert response.status_code == 409
        assert response.data["code"] == "invalid_state_transition"
        assert response.data["detail"] == "A completed campaign cannot be launched again."

    def test_conflict_carries_field_details(self) -> None:
        response = api_exception_handler(
            ConflictError("Duplicate phone number.", details={"phone_number": ["Already exists."]}),
            _context(),
        )
        assert response.status_code == 409
        assert response.data["errors"] == {"phone_number": ["Already exists."]}

    def test_provider_error_defaults_to_502(self) -> None:
        response = api_exception_handler(ProviderError(), _context())
        assert response.status_code == 502
        assert response.data["code"] == "provider_error"

    def test_rate_limit_is_retryable_and_maps_to_429(self) -> None:
        error = ProviderRateLimited(retry_after=30)
        assert error.retryable is True
        assert error.retry_after == 30
        assert api_exception_handler(error, _context()).status_code == 429


class TestDrfErrors:
    def test_validation_errors_use_the_envelope(self) -> None:
        response = api_exception_handler(ValidationError({"name": ["This field is required."]}), _context())
        assert response.status_code == 400
        assert response.data["code"] == "validation_failed"
        assert response.data["errors"] == {"name": ["This field is required."]}

    def test_not_found_uses_the_envelope(self) -> None:
        response = api_exception_handler(NotFound(), _context())
        assert response.status_code == 404
        assert set(response.data) == {"detail", "code", "errors"}

    def test_permission_denied_uses_the_envelope(self) -> None:
        response = api_exception_handler(PermissionDenied(), _context())
        assert response.status_code == 403


class TestUnexpectedErrors:
    def test_internal_errors_are_masked_but_traceable(self) -> None:
        response = api_exception_handler(RuntimeError("token EAAsecret leaked in a traceback"), _context())

        assert response.status_code == 500
        body = str(response.data)
        # The message, and anything it might have contained, stays in the log.
        assert "EAAsecret" not in body
        assert "RuntimeError" not in body
        assert len(response.data["errors"]["incident_id"]) == 12
