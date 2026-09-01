"""The audit trail underpins the compliance story, so it is tested directly."""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from core.audit import client_ip, record_audit
from core.models import AuditAction, AuditLog

pytestmark = pytest.mark.django_db


class TestRecordAudit:
    def test_writes_a_row_with_the_actor_and_target(self, operator) -> None:
        entry = record_audit(
            AuditAction.CONTACT_OPTED_OUT,
            user=operator,
            obj=operator,
            description="Opted out via inbound STOP",
            metadata={"source": "inbound_message"},
        )

        assert entry is not None
        assert entry.user == operator
        assert entry.action == AuditAction.CONTACT_OPTED_OUT
        assert entry.object_type == "User"
        assert entry.object_id == str(operator.pk)
        assert entry.metadata == {"source": "inbound_message"}

    def test_takes_the_actor_from_the_request_when_not_given(self, operator) -> None:
        request = RequestFactory().post("/")
        request.user = operator

        entry = record_audit(AuditAction.CAMPAIGN_LAUNCHED, request=request)

        assert entry.user == operator

    def test_anonymous_request_records_no_user(self, db) -> None:
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().post("/")
        request.user = AnonymousUser()

        entry = record_audit(AuditAction.LOGIN_FAILED, request=request)

        assert entry.user is None

    def test_long_description_is_truncated_rather_than_raising(self, operator) -> None:
        entry = record_audit(AuditAction.LOGIN, user=operator, description="x" * 500)
        assert len(entry.description) == 255

    def test_failure_is_swallowed_so_the_operation_survives(self, operator, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("database is down")

        monkeypatch.setattr(AuditLog.objects, "create", boom)

        assert record_audit(AuditAction.LOGIN, user=operator) is None


class TestClientIp:
    def test_prefers_the_first_forwarded_address(self) -> None:
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
        assert client_ip(request) == "203.0.113.9"

    def test_falls_back_to_remote_addr(self) -> None:
        request = RequestFactory().get("/", REMOTE_ADDR="198.51.100.4")
        assert client_ip(request) == "198.51.100.4"

    def test_handles_no_request(self) -> None:
        assert client_ip(None) is None
