"""HTML pages: the campaign wizard, monitoring, and the template pages."""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from campaigns.models import Campaign, CampaignMessageType, CampaignStatus
from campaigns.services import launch_campaign, set_audience, set_message
from contacts.models import GroupMembership
from messaging.models import Message, MessageStatus
from whatsapp.models import MessageTemplate

pytestmark = pytest.mark.django_db


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


def role_client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


class TestCampaignList:
    def test_requires_authentication(self, client: Client) -> None:
        response = client.get(reverse("campaigns:list"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_renders_campaigns(self, auth_client: Client, make_campaign) -> None:
        make_campaign("Summer Sale")
        body = auth_client.get(reverse("campaigns:list")).content.decode()
        assert "Summer Sale" in body

    def test_empty_state(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("campaigns:list")).content.decode()
        assert "No campaigns yet" in body

    def test_warns_when_no_sender_is_running(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("campaigns:list")).content.decode()
        assert "Sending is unavailable" in body

    def test_no_warning_when_a_sender_is_running(
        self, auth_client: Client, recording_dispatcher
    ) -> None:
        body = auth_client.get(reverse("campaigns:list")).content.decode()
        assert "Sending is unavailable" not in body

    def test_status_filter(self, auth_client: Client, make_campaign) -> None:
        make_campaign("Draft one")
        make_campaign("Finished", status=CampaignStatus.COMPLETED)

        body = auth_client.get(
            reverse("campaigns:list"), {"status": CampaignStatus.COMPLETED}
        ).content.decode()

        assert "Finished" in body
        assert "Draft one" not in body

    def test_viewer_sees_no_create_button(self, viewer) -> None:
        body = role_client(viewer).get(reverse("campaigns:list")).content.decode()
        assert "New campaign" not in body


class TestWizardStepOne:
    def test_creates_a_draft_and_advances(self, auth_client: Client) -> None:
        response = auth_client.post(
            reverse("campaigns:create"), {"name": "Summer Sale", "description": ""}
        )

        campaign = Campaign.objects.get()
        assert response.status_code == 302
        assert response.url == reverse("campaigns:wizard-audience", args=[campaign.pk])
        assert campaign.status == CampaignStatus.DRAFT

    def test_blank_name_re_renders(self, auth_client: Client) -> None:
        response = auth_client.post(reverse("campaigns:create"), {"name": "  "})

        assert response.status_code == 200
        assert Campaign.objects.count() == 0

    def test_viewer_is_denied(self, viewer) -> None:
        response = role_client(viewer).get(reverse("campaigns:create"))
        assert response.status_code == 302
        assert Campaign.objects.count() == 0


class TestWizardStepTwo:
    def test_shows_the_eligibility_breakdown(
        self, auth_client: Client, make_campaign, group, make_contact
    ) -> None:
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=True))
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=False))
        campaign = make_campaign()
        set_audience(campaign, [group])

        response = auth_client.get(reverse("campaigns:wizard-audience", args=[campaign.pk]))

        assert response.status_code == 200
        assert response.context["breakdown"].eligible == 1
        assert response.context["breakdown"].excluded_not_opted_in == 1

    def test_selecting_a_group_advances(
        self, auth_client: Client, make_campaign, group
    ) -> None:
        campaign = make_campaign()

        response = auth_client.post(
            reverse("campaigns:wizard-audience", args=[campaign.pk]),
            {"groups": [str(group.pk)]},
        )

        assert response.status_code == 302
        assert campaign.audience_groups.count() == 1

    def test_no_selection_re_renders(self, auth_client: Client, make_campaign) -> None:
        campaign = make_campaign()
        response = auth_client.post(
            reverse("campaigns:wizard-audience", args=[campaign.pk]), {}
        )

        assert response.status_code == 200
        assert campaign.audience_groups.count() == 0

    def test_target_all_eligible_is_accepted(
        self, auth_client: Client, make_campaign
    ) -> None:
        campaign = make_campaign()
        auth_client.post(
            reverse("campaigns:wizard-audience", args=[campaign.pk]),
            {"target_all_eligible": "on"},
        )

        campaign.refresh_from_db()
        assert campaign.target_all_eligible is True


class TestWizardStepThree:
    def test_loading_a_template_shows_its_variables_without_advancing(
        self, auth_client: Client, make_campaign, approved_template
    ) -> None:
        """Choosing a template must reveal its variable rows, not skip the step."""
        campaign = make_campaign()

        response = auth_client.post(
            reverse("campaigns:wizard-message", args=[campaign.pk]),
            {
                "message_type": "template",
                "template": str(approved_template.pk),
                "reload_template": "1",
            },
        )

        assert response.status_code == 200
        assert "var_value_order_id" in response.content.decode()
        campaign.refresh_from_db()
        assert campaign.template is None

    def test_saving_a_complete_mapping_advances(
        self, auth_client: Client, make_campaign, approved_template
    ) -> None:
        campaign = make_campaign()

        response = auth_client.post(
            reverse("campaigns:wizard-message", args=[campaign.pk]),
            {
                "message_type": "template",
                "template": str(approved_template.pk),
                "var_source_name": "name",
                "var_value_order_id": "A-100",
            },
        )

        assert response.status_code == 302
        campaign.refresh_from_db()
        assert campaign.template == approved_template
        assert campaign.variable_mapping["name"]["source"] == "contact_field"
        assert campaign.variable_mapping["order_id"]["value"] == "A-100"

    def test_unmapped_variable_re_renders_with_an_error(
        self, auth_client: Client, make_campaign, approved_template
    ) -> None:
        campaign = make_campaign()

        response = auth_client.post(
            reverse("campaigns:wizard-message", args=[campaign.pk]),
            {
                "message_type": "template",
                "template": str(approved_template.pk),
                "var_source_name": "name",
            },
        )

        assert response.status_code == 200
        assert "var_value_order_id" in response.context["form"].errors


class TestWizardStepFour:
    def test_preview_shows_counts_and_the_rendered_message(
        self, auth_client: Client, ready_campaign
    ) -> None:
        response = auth_client.get(
            reverse("campaigns:wizard-preview", args=[ready_campaign.pk])
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert response.context["preview"].audience.eligible == 3
        assert "A-100" in body

    def test_continue_is_disabled_without_a_sender(
        self, auth_client: Client, ready_campaign
    ) -> None:
        body = auth_client.get(
            reverse("campaigns:wizard-preview", args=[ready_campaign.pk])
        ).content.decode()

        assert "Sending is unavailable" in body
        assert "disabled" in body

    def test_blockers_are_listed(self, auth_client: Client, make_campaign) -> None:
        body = auth_client.get(
            reverse("campaigns:wizard-preview", args=[make_campaign().pk])
        ).content.decode()

        assert "cannot be sent yet" in body


class TestWizardStepFive:
    def test_launch_creates_recipients_and_redirects(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        response = auth_client.post(
            reverse("campaigns:wizard-confirm", args=[ready_campaign.pk]), {"confirm": "on"}
        )

        assert response.status_code == 302
        assert response.url == reverse("campaigns:detail", args=[ready_campaign.pk])
        assert Message.objects.count() == 3

    def test_confirmation_checkbox_is_required(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        response = auth_client.post(
            reverse("campaigns:wizard-confirm", args=[ready_campaign.pk]), {}
        )

        assert response.status_code == 200
        assert Message.objects.count() == 0

    def test_launching_without_a_sender_is_refused(
        self, auth_client: Client, ready_campaign
    ) -> None:
        response = auth_client.post(
            reverse("campaigns:wizard-confirm", args=[ready_campaign.pk]), {"confirm": "on"}
        )

        assert response.status_code == 302
        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.DRAFT
        assert Message.objects.count() == 0

    def test_viewer_cannot_reach_the_confirm_step(
        self, viewer, ready_campaign, recording_dispatcher
    ) -> None:
        """Sending is the one irreversible action; a Viewer must never reach it."""
        response = role_client(viewer).get(
            reverse("campaigns:wizard-confirm", args=[ready_campaign.pk])
        )

        assert response.status_code == 302
        assert Message.objects.count() == 0

    def test_csrf_token_is_required(self, operator, ready_campaign, recording_dispatcher) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(operator)

        response = csrf_client.post(
            reverse("campaigns:wizard-confirm", args=[ready_campaign.pk]), {"confirm": "on"}
        )

        assert response.status_code == 403
        assert Message.objects.count() == 0


class TestCampaignDetail:
    def test_shows_readiness_for_a_draft(self, auth_client: Client, ready_campaign) -> None:
        body = auth_client.get(
            reverse("campaigns:detail", args=[ready_campaign.pk])
        ).content.decode()

        assert "Ready to send" in body

    def test_shows_progress_after_launch(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        launch_campaign(ready_campaign)

        response = auth_client.get(reverse("campaigns:detail", args=[ready_campaign.pk]))

        assert response.context["stats"].total == 3
        assert response.context["stats"].pending == 3

    def test_lists_failed_messages(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        launch_campaign(ready_campaign)
        Message.objects.update(
            status=MessageStatus.FAILED, error_message="Not a WhatsApp user"
        )

        body = auth_client.get(
            reverse("campaigns:detail", args=[ready_campaign.pk])
        ).content.decode()

        assert "Not a WhatsApp user" in body

    def test_groups_failures_by_the_provider_error(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        """
        The per-recipient list cannot show whether this is one bad number or
        something systemic; the grouped breakdown is what answers that.
        """
        launch_campaign(ready_campaign)
        Message.objects.update(
            status=MessageStatus.FAILED,
            error_code="131026",
            error_message="Message undeliverable",
        )

        response = auth_client.get(reverse("campaigns:detail", args=[ready_campaign.pk]))

        reasons = response.context["failure_reasons"]
        assert [(r.error_code, r.count) for r in reasons] == [("131026", 3)]
        assert "Failure reasons" in response.content.decode()

    def test_a_campaign_with_no_failures_shows_no_breakdown(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        launch_campaign(ready_campaign)

        body = auth_client.get(
            reverse("campaigns:detail", args=[ready_campaign.pk])
        ).content.decode()

        assert "Failure reasons" not in body

    def test_recipients_can_be_exported(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        launch_campaign(ready_campaign)

        body = auth_client.get(
            reverse("campaigns:detail", args=[ready_campaign.pk])
        ).content.decode()

        assert reverse("dashboard:campaign-recipients-report", args=[ready_campaign.pk]) in body

    def test_a_campaign_with_no_recipients_offers_no_export(
        self, auth_client: Client, ready_campaign
    ) -> None:
        """There is nothing to export from a plan that has not been sent."""
        body = auth_client.get(
            reverse("campaigns:detail", args=[ready_campaign.pk])
        ).content.decode()

        assert reverse("dashboard:campaign-recipients-report", args=[ready_campaign.pk]) not in body

    def test_pause_and_resume_buttons_follow_the_state_machine(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        launch_campaign(ready_campaign)

        auth_client.post(reverse("campaigns:action", args=[ready_campaign.pk, "pause"]))
        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.PAUSED

        auth_client.post(reverse("campaigns:action", args=[ready_campaign.pk, "resume"]))
        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.PROCESSING

    def test_illegal_action_shows_an_error_rather_than_crashing(
        self, auth_client: Client, ready_campaign
    ) -> None:
        response = auth_client.post(
            reverse("campaigns:action", args=[ready_campaign.pk, "pause"]), follow=True
        )

        assert response.status_code == 200
        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.DRAFT

    def test_viewer_cannot_cancel(self, viewer, ready_campaign, recording_dispatcher) -> None:
        launch_campaign(ready_campaign)

        role_client(viewer).post(
            reverse("campaigns:action", args=[ready_campaign.pk, "cancel"])
        )

        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.PROCESSING


class TestCampaignMessages:
    def test_lists_recipients(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        launch_campaign(ready_campaign)

        response = auth_client.get(reverse("campaigns:messages", args=[ready_campaign.pk]))

        assert response.status_code == 200
        assert response.context["page_obj"].paginator.count == 3

    def test_status_filter(
        self, auth_client: Client, ready_campaign, recording_dispatcher
    ) -> None:
        launch_campaign(ready_campaign)
        Message.objects.update(status=MessageStatus.FAILED)

        response = auth_client.get(
            reverse("campaigns:messages", args=[ready_campaign.pk]), {"status": "failed"}
        )

        assert response.context["page_obj"].paginator.count == 3


class TestTemplatePages:
    def test_list_renders(self, auth_client: Client, approved_template) -> None:
        body = auth_client.get(reverse("whatsapp:template-list")).content.decode()
        assert "order_ready" in body

    def test_list_explains_that_meta_owns_approval(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("whatsapp:template-list")).content.decode()
        assert "created and approved by Meta" in body

    def test_detail_shows_a_rendered_preview(
        self, auth_client: Client, approved_template
    ) -> None:
        body = auth_client.get(
            reverse("whatsapp:template-detail", args=[approved_template.pk])
        ).content.decode()

        assert "Preview" in body
        assert "{{" not in body.split("chat-bubble")[1][:400]

    def test_local_template_detail_warns_it_is_not_approved(
        self, auth_client: Client, local_template
    ) -> None:
        with override_settings(WHATSAPP_PROVIDER="meta"):
            body = auth_client.get(
                reverse("whatsapp:template-detail", args=[local_template.pk])
            ).content.decode()

        assert "Not usable for sending" in body

    def test_operator_cannot_create_a_template(self, auth_client: Client) -> None:
        response = auth_client.get(reverse("whatsapp:template-create"))
        assert response.status_code == 302

    def test_administrator_can_create_a_local_template(self, administrator) -> None:
        response = role_client(administrator).post(
            reverse("whatsapp:template-create"),
            {
                "name": "Order Ready",
                "language": "en_US",
                "category": "utility",
                "header_text": "",
                "body_text": "Hello {{name}}, order {{order_id}} is ready.",
                "footer_text": "",
            },
        )

        assert response.status_code == 302
        template = MessageTemplate.objects.get()
        assert template.name == "order_ready"
        assert template.source == "local"
        assert template.variables == ["name", "order_id"]

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_local_creation_is_blocked_under_the_live_provider(self, administrator) -> None:
        response = role_client(administrator).get(reverse("whatsapp:template-create"))

        assert response.status_code == 302
        assert MessageTemplate.objects.count() == 0


class TestNavigation:
    def test_sidebar_links_to_campaigns_and_templates(self, auth_client: Client) -> None:
        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert reverse("campaigns:list") in body
        assert reverse("whatsapp:template-list") in body
