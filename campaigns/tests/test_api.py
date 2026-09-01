"""REST API for campaigns, templates and messages."""

from __future__ import annotations

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from campaigns.models import Campaign, CampaignMessageType, CampaignStatus
from campaigns.services import set_audience, set_message
from contacts.models import GroupMembership
from messaging.models import Message, MessageStatus
from whatsapp.models import MessageTemplate

pytestmark = pytest.mark.django_db

LIST_URL = reverse("campaigns-api:campaign-list")


@pytest.fixture
def ready_campaign(make_campaign, approved_template, group, make_contact):
    for index in range(3):
        GroupMembership.objects.create(
            group=group, contact=make_contact(f"Member {index}", opted_in=True)
        )

    campaign = make_campaign("Summer Sale")
    set_audience(campaign, [group])
    set_message(
        campaign,
        message_type=CampaignMessageType.TEMPLATE,
        template=approved_template,
        variable_mapping={
            "name": {"source": "contact_field", "value": "name"},
            "order_id": {"source": "literal", "value": "A-100"},
        },
    )
    return campaign


def role_client(user) -> APIClient:
    client = APIClient()
    client.force_login(user)
    return client


class TestCampaignCrud:
    def test_requires_authentication(self, api_client: APIClient) -> None:
        assert api_client.get(LIST_URL).status_code in (401, 403)

    def test_create_returns_a_draft(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(LIST_URL, {"name": "Launch week"}, format="json")

        assert response.status_code == 201
        assert Campaign.objects.get().status == CampaignStatus.DRAFT

    def test_blank_name_is_rejected(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(LIST_URL, {"name": "   "}, format="json")
        assert response.status_code == 400

    def test_status_is_read_only(self, auth_api_client: APIClient, make_campaign) -> None:
        """A writable status would let a client skip the state machine entirely."""
        campaign = make_campaign()
        url = reverse("campaigns-api:campaign-detail", args=[campaign.pk])

        auth_api_client.patch(url, {"status": CampaignStatus.COMPLETED}, format="json")

        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.DRAFT

    def test_a_sending_campaign_cannot_be_edited(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)
        url = reverse("campaigns-api:campaign-detail", args=[ready_campaign.pk])

        response = auth_api_client.patch(url, {"name": "Renamed"}, format="json")

        assert response.status_code == 409

    def test_filter_by_status(self, auth_api_client: APIClient, make_campaign) -> None:
        make_campaign("Draft one")
        make_campaign("Done", status=CampaignStatus.COMPLETED)

        response = auth_api_client.get(LIST_URL, {"status": CampaignStatus.COMPLETED})

        assert response.data["count"] == 1


class TestWizardEndpoints:
    def test_audience_endpoint_returns_the_breakdown(
        self, auth_api_client: APIClient, make_campaign, group, make_contact
    ) -> None:
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=True))
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=False))
        campaign = make_campaign()
        url = reverse("campaigns-api:campaign-audience", args=[campaign.pk])

        response = auth_api_client.put(url, {"group_ids": [str(group.pk)]}, format="json")

        assert response.status_code == 200
        assert response.data["in_audience"] == 2
        assert response.data["eligible"] == 1
        assert response.data["excluded_not_opted_in"] == 1

    def test_audience_requires_a_selection(
        self, auth_api_client: APIClient, make_campaign
    ) -> None:
        url = reverse("campaigns-api:campaign-audience", args=[make_campaign().pk])
        response = auth_api_client.put(url, {"group_ids": []}, format="json")
        assert response.status_code == 400

    def test_message_endpoint_stores_the_mapping(
        self, auth_api_client: APIClient, make_campaign, approved_template
    ) -> None:
        campaign = make_campaign()
        url = reverse("campaigns-api:campaign-message", args=[campaign.pk])

        response = auth_api_client.put(
            url,
            {
                "message_type": "template",
                "template": str(approved_template.pk),
                "variable_mapping": {
                    "name": {"source": "contact_field", "value": "name"},
                    "order_id": {"source": "literal", "value": "A-1"},
                },
            },
            format="json",
        )

        assert response.status_code == 200
        campaign.refresh_from_db()
        assert campaign.template == approved_template

    def test_incomplete_mapping_is_rejected(
        self, auth_api_client: APIClient, make_campaign, approved_template
    ) -> None:
        url = reverse("campaigns-api:campaign-message", args=[make_campaign().pk])

        response = auth_api_client.put(
            url,
            {"message_type": "template", "template": str(approved_template.pk),
             "variable_mapping": {}},
            format="json",
        )

        assert response.status_code == 400
        assert "order_id" in response.data["errors"]

    def test_preview_reports_readiness(
        self, auth_api_client: APIClient, ready_campaign
    ) -> None:
        url = reverse("campaigns-api:campaign-preview", args=[ready_campaign.pk])

        response = auth_api_client.get(url)

        assert response.status_code == 200
        assert response.data["recipient_count"] == 3
        assert response.data["is_ready"] is True
        assert response.data["blockers"] == []
        assert "A-100" in response.data["sample_text"]

    def test_preview_lists_blockers(
        self, auth_api_client: APIClient, make_campaign
    ) -> None:
        url = reverse("campaigns-api:campaign-preview", args=[make_campaign().pk])

        response = auth_api_client.get(url)

        assert response.data["is_ready"] is False
        assert len(response.data["blockers"]) >= 1


class TestLaunch:
    def test_launch_materializes_recipients(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        url = reverse("campaigns-api:campaign-launch", args=[ready_campaign.pk])

        response = auth_api_client.post(url, {"confirm": True}, format="json")

        assert response.status_code == 202
        assert Message.objects.filter(campaign=ready_campaign).count() == 3

    def test_confirmation_is_mandatory(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        """An API client must not be able to skip the acknowledgement."""
        url = reverse("campaigns-api:campaign-launch", args=[ready_campaign.pk])

        response = auth_api_client.post(url, {"confirm": False}, format="json")

        assert response.status_code == 400
        assert Message.objects.count() == 0

    def test_launch_without_a_sender_returns_503(
        self, auth_api_client: APIClient, ready_campaign
    ) -> None:
        url = reverse("campaigns-api:campaign-launch", args=[ready_campaign.pk])

        response = auth_api_client.post(url, {"confirm": True}, format="json")

        assert response.status_code == 503
        assert response.data["code"] == "sending_unavailable"
        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.DRAFT

    def test_invalid_campaign_returns_400_with_blockers(
        self, auth_api_client: APIClient, make_campaign, recording_dispatcher
    ) -> None:
        url = reverse("campaigns-api:campaign-launch", args=[make_campaign().pk])

        response = auth_api_client.post(url, {"confirm": True}, format="json")

        assert response.status_code == 400
        assert response.data["errors"]["blockers"]

    def test_relaunching_a_completed_campaign_returns_409(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        ready_campaign.status = CampaignStatus.COMPLETED
        ready_campaign.save(update_fields=["status"])
        url = reverse("campaigns-api:campaign-launch", args=[ready_campaign.pk])

        response = auth_api_client.post(url, {"confirm": True}, format="json")

        assert response.status_code == 409


class TestLifecycleEndpoints:
    def test_pause_and_resume(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)

        pause = auth_api_client.post(
            reverse("campaigns-api:campaign-pause", args=[ready_campaign.pk])
        )
        assert pause.data["status"] == CampaignStatus.PAUSED

        resume = auth_api_client.post(
            reverse("campaigns-api:campaign-resume", args=[ready_campaign.pk])
        )
        assert resume.data["status"] == CampaignStatus.PROCESSING

    def test_pausing_a_draft_returns_409(
        self, auth_api_client: APIClient, ready_campaign
    ) -> None:
        response = auth_api_client.post(
            reverse("campaigns-api:campaign-pause", args=[ready_campaign.pk])
        )
        assert response.status_code == 409

    def test_cancel_abandons_unsent_messages(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)

        response = auth_api_client.post(
            reverse("campaigns-api:campaign-cancel", args=[ready_campaign.pk])
        )

        assert response.data["status"] == CampaignStatus.CANCELLED
        assert Message.objects.filter(status=MessageStatus.FAILED).count() == 3


class TestMonitoringEndpoints:
    def test_stats_endpoint(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)

        response = auth_api_client.get(
            reverse("campaigns-api:campaign-stats", args=[ready_campaign.pk])
        )

        assert response.status_code == 200
        assert response.data["total"] == 3
        assert response.data["pending"] == 3
        assert response.data["status"] == CampaignStatus.PROCESSING

    def test_messages_endpoint_lists_recipients(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)

        response = auth_api_client.get(
            reverse("campaigns-api:campaign-messages", args=[ready_campaign.pk])
        )

        assert response.status_code == 200
        assert response.data["count"] == 3

    def test_messages_endpoint_filters_by_status(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)
        Message.objects.filter(campaign=ready_campaign).update(status=MessageStatus.FAILED)

        response = auth_api_client.get(
            reverse("campaigns-api:campaign-messages", args=[ready_campaign.pk]),
            {"status": "failed"},
        )

        assert response.data["count"] == 3


class TestTemplateApi:
    def test_list_templates(self, auth_api_client: APIClient, approved_template) -> None:
        response = auth_api_client.get(reverse("whatsapp-api:template-list"))

        assert response.status_code == 200
        assert response.data["count"] == 1
        assert response.data["results"][0]["is_usable"] is True

    def test_usable_filter_respects_the_provider(
        self, auth_api_client: APIClient, approved_template, local_template
    ) -> None:
        response = auth_api_client.get(reverse("whatsapp-api:template-list"), {"usable": "true"})
        assert response.data["count"] == 2

    def test_render_endpoint_previews_safely(
        self, auth_api_client: APIClient, approved_template
    ) -> None:
        url = reverse("whatsapp-api:template-render", args=[approved_template.pk])

        response = auth_api_client.post(
            url, {"values": {"name": "Aarav", "order_id": "A-1"}}, format="json"
        )

        assert response.status_code == 200
        assert "Aarav" in response.data["full_text"]
        assert response.data["is_complete"] is True

    def test_render_reports_missing_values(
        self, auth_api_client: APIClient, approved_template
    ) -> None:
        url = reverse("whatsapp-api:template-render", args=[approved_template.pk])

        response = auth_api_client.post(url, {"values": {"name": "Aarav"}}, format="json")

        assert response.data["missing"] == ["order_id"]
        assert response.data["is_complete"] is False

    def test_operator_cannot_create_a_template(self, auth_api_client: APIClient) -> None:
        response = auth_api_client.post(
            reverse("whatsapp-api:template-list"),
            {"name": "sneaky", "body_text": "Hi"},
            format="json",
        )
        assert response.status_code == 403

    def test_administrator_can_create_a_local_template(self, administrator) -> None:
        response = role_client(administrator).post(
            reverse("whatsapp-api:template-list"),
            {"name": "Dev Promo", "body_text": "Hi {{name}}"},
            format="json",
        )

        assert response.status_code == 201
        template = MessageTemplate.objects.get()
        assert template.source == "local"
        assert template.status == "not_submitted"
        assert template.name == "dev_promo"

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_local_templates_cannot_be_created_under_the_live_provider(
        self, administrator
    ) -> None:
        """Nothing may create something that looks approved when it is not."""
        response = role_client(administrator).post(
            reverse("whatsapp-api:template-list"),
            {"name": "sneaky", "body_text": "Hi"},
            format="json",
        )

        assert response.status_code == 400
        assert MessageTemplate.objects.count() == 0

    def test_templates_cannot_be_edited_or_deleted(
        self, administrator, approved_template
    ) -> None:
        client = role_client(administrator)
        url = reverse("whatsapp-api:template-detail", args=[approved_template.pk])

        assert client.patch(url, {"body_text": "changed"}, format="json").status_code == 405
        assert client.delete(url).status_code == 405

    def test_sync_reports_that_it_is_not_wired_up(self, administrator) -> None:
        response = role_client(administrator).post(reverse("whatsapp-api:template-sync"))

        assert response.status_code == 503
        assert response.data["code"] == "provider_not_configured"


class TestMessageApi:
    def test_messages_are_read_only(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)
        message = Message.objects.first()
        url = reverse("messaging-api:message-detail", args=[message.pk])

        assert auth_api_client.patch(url, {"status": "sent"}, format="json").status_code == 405
        assert auth_api_client.delete(url).status_code == 405

    def test_detail_includes_the_status_history(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign
        from messaging.services import StatusUpdate, apply_status_update

        launch_campaign(ready_campaign)
        message = Message.objects.first()
        apply_status_update(message, StatusUpdate(status=MessageStatus.SENT))

        response = auth_api_client.get(
            reverse("messaging-api:message-detail", args=[message.pk])
        )

        assert response.status_code == 200
        assert len(response.data["status_events"]) == 1

    def test_global_stats_endpoint(
        self, auth_api_client: APIClient, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)

        response = auth_api_client.get(reverse("messaging-api:message-stats"))

        assert response.status_code == 200
        assert response.data["total"] == 3
        assert response.data["pending"] == 3


class TestAuthorization:
    def test_viewer_can_read_campaigns(self, viewer, make_campaign) -> None:
        make_campaign()
        assert role_client(viewer).get(LIST_URL).status_code == 200

    def test_viewer_cannot_create_a_campaign(self, viewer) -> None:
        response = role_client(viewer).post(LIST_URL, {"name": "Nope"}, format="json")
        assert response.status_code == 403
        assert Campaign.objects.count() == 0

    def test_viewer_cannot_launch(self, viewer, ready_campaign, recording_dispatcher) -> None:
        """Sending is the one irreversible action; a Viewer must never reach it."""
        url = reverse("campaigns-api:campaign-launch", args=[ready_campaign.pk])

        response = role_client(viewer).post(url, {"confirm": True}, format="json")

        assert response.status_code == 403
        assert Message.objects.count() == 0

    def test_viewer_cannot_cancel(self, viewer, ready_campaign, recording_dispatcher) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)
        url = reverse("campaigns-api:campaign-cancel", args=[ready_campaign.pk])

        assert role_client(viewer).post(url).status_code == 403

    def test_viewer_can_read_messages(
        self, viewer, ready_campaign, recording_dispatcher
    ) -> None:
        from campaigns.services import launch_campaign

        launch_campaign(ready_campaign)
        assert role_client(viewer).get(reverse("messaging-api:message-list")).status_code == 200
