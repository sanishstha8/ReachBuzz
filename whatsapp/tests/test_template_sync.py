"""
Mirroring Meta's template registry.

The rule this has to respect is the one the project does not bend: template
approval belongs to Meta. Sync is the *only* place the application writes an
approval status, and all it may do is copy what Meta reports. Several tests
here exist to prove it cannot do anything else.
"""

from __future__ import annotations

import json

import pytest
import responses
from django.urls import reverse

from core.exceptions import ProviderError, ProviderNotConfigured
from core.models import AuditAction, AuditLog
from whatsapp.models import MessageTemplate, TemplateSource, TemplateStatus
from whatsapp.services.templates import sync_templates_from_provider

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "https://graph.facebook.com/vTEST/456/message_templates"


@pytest.fixture
def meta(settings):
    settings.WHATSAPP_PROVIDER = "meta"
    settings.META_API_VERSION = "vTEST"
    settings.META_ACCESS_TOKEN = "EAAtoken"
    settings.META_PHONE_NUMBER_ID = "123"
    settings.META_WABA_ID = "456"
    return settings


def meta_template(**overrides) -> dict:
    template = {
        "name": "order_ready",
        "language": "en_US",
        "status": "APPROVED",
        "category": "UTILITY",
        "id": "1667192013751005",
        "components": [{"type": "BODY", "text": "Hello {{name}}, order {{order_id}} is ready."}],
    }
    template.update(overrides)
    return template


def stub(http, *templates) -> None:
    http.add(responses.GET, TEMPLATES_URL, json={"data": list(templates)}, status=200)


class TestMockProvider:
    def test_sync_is_refused_rather_than_reporting_zero_templates(self, organization) -> None:
        """
        "0 synced" would read as *you have none* rather than *there was nothing
        to ask*. The mock has no upstream registry at all.
        """
        with pytest.raises(ProviderNotConfigured, match="mock"):
            sync_templates_from_provider(organization=organization)


class TestSync:
    def test_a_template_is_created_from_the_provider(self, meta, http, organization) -> None:
        stub(http, meta_template())

        assert sync_templates_from_provider(organization=organization) == 1

        template = MessageTemplate.objects.get()
        assert template.name == "order_ready"
        assert template.language == "en_US"
        assert template.source == TemplateSource.SYNCED
        assert template.status == TemplateStatus.APPROVED
        assert template.provider_template_id == "1667192013751005"

    def test_the_variables_are_read_out_of_the_body(self, meta, http, organization) -> None:
        stub(http, meta_template())

        sync_templates_from_provider(organization=organization)

        assert MessageTemplate.objects.get().variables == ["name", "order_id"]

    def test_re_syncing_updates_rather_than_duplicating(self, meta, http, organization) -> None:
        stub(http, meta_template())
        sync_templates_from_provider(organization=organization)

        stub(http, meta_template(status="PAUSED"))
        sync_templates_from_provider(organization=organization)

        assert MessageTemplate.objects.count() == 1
        assert MessageTemplate.objects.get().status == TemplateStatus.PAUSED

    def test_the_same_name_in_another_language_is_a_separate_template(self, meta, http, organization) -> None:
        stub(http, meta_template(), meta_template(language="ne", id="222"))

        assert sync_templates_from_provider(organization=organization) == 2
        assert MessageTemplate.objects.count() == 2

    def test_a_template_with_no_name_is_skipped_not_stored(self, meta, http, organization) -> None:
        stub(http, meta_template(name=""))

        assert sync_templates_from_provider(organization=organization) == 0
        assert MessageTemplate.objects.count() == 0

    def test_the_sync_is_audited(self, meta, http, operator, organization) -> None:
        stub(http, meta_template())

        sync_templates_from_provider(organization=organization, user=operator)

        entry = AuditLog.objects.get(action=AuditAction.TEMPLATES_SYNCED)
        assert entry.user == operator
        assert entry.metadata["count"] == 1

    def test_a_provider_error_raises_rather_than_reporting_zero(self, meta, http, organization) -> None:
        http.add(responses.GET, TEMPLATES_URL, json={"error": {"code": 190}}, status=401)

        with pytest.raises(ProviderError):
            sync_templates_from_provider(organization=organization)

    def test_nothing_is_written_when_the_provider_fails(self, meta, http, organization) -> None:
        http.add(responses.GET, TEMPLATES_URL, json={"error": {"code": 190}}, status=401)

        with pytest.raises(ProviderError):
            sync_templates_from_provider(organization=organization)

        assert MessageTemplate.objects.count() == 0


class TestApprovalBelongsToMeta:
    def test_only_what_meta_reports_as_approved_becomes_approved(self, meta, http, organization) -> None:
        stub(
            http,
            meta_template(name="approved_one", status="APPROVED", id="1"),
            meta_template(name="pending_one", status="PENDING", id="2"),
            meta_template(name="rejected_one", status="REJECTED", id="3"),
        )

        sync_templates_from_provider(organization=organization)

        statuses = dict(MessageTemplate.objects.values_list("name", "status"))
        assert statuses == {
            "approved_one": TemplateStatus.APPROVED,
            "pending_one": TemplateStatus.PENDING,
            "rejected_one": TemplateStatus.REJECTED,
        }

    def test_a_template_meta_has_not_approved_stays_unusable(self, meta, http, organization) -> None:
        stub(http, meta_template(status="PENDING"))

        sync_templates_from_provider(organization=organization)

        assert MessageTemplate.objects.get().is_usable("meta") is False

    def test_a_local_template_is_replaced_by_metas_version_of_it(self, meta, http, organization) -> None:
        """
        Once Meta has a template by that name, Meta's is the truth. Leaving a
        local stub shadowing it would let someone send a draft believing it
        had been approved.
        """
        MessageTemplate.objects.create(
            name="order_ready",
            language="en_US",
            source=TemplateSource.LOCAL,
            status=TemplateStatus.NOT_SUBMITTED,
            body_text="a local draft",
            organization=organization,
        )
        stub(http, meta_template())

        sync_templates_from_provider(organization=organization)

        template = MessageTemplate.objects.get()
        assert template.source == TemplateSource.SYNCED
        assert template.status == TemplateStatus.APPROVED
        assert template.body_text != "a local draft"

    def test_an_unfamiliar_state_never_becomes_approved(self, meta, http, organization) -> None:
        stub(http, meta_template(status="A_STATE_META_ADDED_LATER"))

        sync_templates_from_provider(organization=organization)

        assert MessageTemplate.objects.get().status == TemplateStatus.DISABLED


class TestSyncEndpoint:
    """
    Sync is administrator-only, which is the existing policy for anything that
    writes template state: one call rewrites the approval status of every
    template, and an operator sending campaigns should not be able to change
    what counts as approved.
    """

    def admin_client(self, administrator):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_login(administrator)
        return client

    def test_an_administrator_can_trigger_a_sync(self, meta, http, administrator) -> None:
        stub(http, meta_template())

        response = self.admin_client(administrator).post(reverse("whatsapp-api:template-sync"))

        assert response.status_code == 200
        assert response.json()["synced"] == 1

    def test_the_mock_provider_is_refused_with_an_explanation(self, administrator) -> None:
        response = self.admin_client(administrator).post(reverse("whatsapp-api:template-sync"))

        assert response.status_code == 503
        assert "mock" in json.dumps(response.json())

    def test_an_operator_cannot_trigger_a_sync(self, meta, auth_api_client) -> None:
        assert auth_api_client.post(reverse("whatsapp-api:template-sync")).status_code == 403

    def test_a_viewer_cannot_trigger_a_sync(self, meta, viewer) -> None:
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_login(viewer)

        assert client.post(reverse("whatsapp-api:template-sync")).status_code == 403
