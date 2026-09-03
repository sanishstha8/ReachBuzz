"""
Campaign services: audience resolution, validation, state machine, launch.

The consent rule gets the closest scrutiny — an audience-resolution bug would
message people who never agreed to hear from us.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from campaigns import dispatch, services
from campaigns.dispatch import SendingUnavailable
from campaigns.models import Campaign, CampaignMessageType, CampaignStatus
from contacts.models import ContactStatus, GroupMembership
from core.exceptions import InvalidStateTransition, ValidationFailed
from core.models import AuditAction, AuditLog
from messaging.models import Message, MessageStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def ready_campaign(make_campaign, approved_template, group, make_contact):
    """A campaign that passes validation: consenting audience, mapped template."""
    for index in range(3):
        contact = make_contact(f"Member {index}", opted_in=True)
        GroupMembership.objects.create(group=group, contact=contact)

    campaign = make_campaign("Summer Sale")
    services.set_audience(campaign, [group])
    services.set_message(
        campaign,
        message_type=CampaignMessageType.TEMPLATE,
        template=approved_template,
        variable_mapping={
            "name": {"source": "contact_field", "value": "name"},
            "order_id": {"source": "literal", "value": "A-100"},
        },
    )
    return campaign


class TestAudienceResolution:
    def test_only_consenting_active_contacts_are_resolved(
        self, make_campaign, group, make_contact
    ) -> None:
        consenting = make_contact("Yes", opted_in=True)
        GroupMembership.objects.create(group=group, contact=consenting)
        GroupMembership.objects.create(
            group=group, contact=make_contact("No consent", opted_in=False)
        )
        GroupMembership.objects.create(
            group=group,
            contact=make_contact("Inactive", opted_in=True, status=ContactStatus.INACTIVE),
        )

        campaign = make_campaign()
        services.set_audience(campaign, [group])

        assert list(services.resolve_audience(campaign)) == [consenting]

    def test_a_contact_in_two_groups_is_resolved_once(
        self, make_campaign, make_contact, organization
    ) -> None:
        from contacts.models import ContactGroup

        first = ContactGroup.objects.create(name="A", organization=organization)
        second = ContactGroup.objects.create(name="B", organization=organization)
        contact = make_contact(opted_in=True)
        GroupMembership.objects.create(group=first, contact=contact)
        GroupMembership.objects.create(group=second, contact=contact)

        campaign = make_campaign()
        services.set_audience(campaign, [first, second])

        assert services.resolve_audience(campaign).count() == 1

    def test_target_all_eligible_covers_every_consenting_contact(
        self, make_campaign, make_contact
    ) -> None:
        make_contact("Yes 1", opted_in=True)
        make_contact("Yes 2", opted_in=True)
        make_contact("No", opted_in=False)

        campaign = make_campaign()
        services.set_audience(campaign, [], target_all_eligible=True)

        assert services.resolve_audience(campaign).count() == 2

    def test_empty_audience_resolves_to_nothing(self, make_campaign) -> None:
        assert services.resolve_audience(make_campaign()).count() == 0

    def test_breakdown_explains_every_exclusion(
        self, make_campaign, group, make_contact
    ) -> None:
        GroupMembership.objects.create(group=group, contact=make_contact("Yes", opted_in=True))
        GroupMembership.objects.create(group=group, contact=make_contact("No", opted_in=False))
        GroupMembership.objects.create(
            group=group,
            contact=make_contact("Blocked", opted_in=True, status=ContactStatus.BLOCKED),
        )

        campaign = make_campaign()
        services.set_audience(campaign, [group])
        breakdown = services.audience_breakdown(campaign)

        assert breakdown.in_audience == 3
        assert breakdown.eligible == 1
        assert breakdown.excluded_not_opted_in == 1
        assert breakdown.excluded_inactive == 1
        assert breakdown.excluded_total == 2

    def test_audience_cannot_be_changed_after_launch(
        self, ready_campaign, group, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign)
        with pytest.raises(InvalidStateTransition):
            services.set_audience(ready_campaign, [group])


class TestMessageContent:
    def test_variable_mapping_is_validated(self, make_campaign, approved_template) -> None:
        campaign = make_campaign()
        with pytest.raises(ValidationFailed) as exc_info:
            services.set_message(
                campaign,
                message_type=CampaignMessageType.TEMPLATE,
                template=approved_template,
                variable_mapping={"name": {"source": "contact_field", "value": "name"}},
            )
        assert "order_id" in exc_info.value.details

    def test_contact_fields_are_restricted_to_an_allow_list(
        self, make_campaign, approved_template
    ) -> None:
        """A template variable must never be able to read an arbitrary attribute."""
        campaign = make_campaign()
        with pytest.raises(ValidationFailed) as exc_info:
            services.set_message(
                campaign,
                message_type=CampaignMessageType.TEMPLATE,
                template=approved_template,
                variable_mapping={
                    "name": {"source": "contact_field", "value": "password"},
                    "order_id": {"source": "literal", "value": "A-1"},
                },
            )
        assert "not a permitted contact field" in str(exc_info.value.details["name"])

    def test_renders_per_recipient(self, ready_campaign, make_contact) -> None:
        contact = make_contact("Aarav Sharma", opted_in=True)
        rendered = services.render_for_contact(ready_campaign, contact)

        assert "Aarav Sharma" in rendered["text"]
        assert "A-100" in rendered["text"]
        assert rendered["missing"] == []

    def test_free_form_text_requires_a_body(self, make_campaign) -> None:
        with pytest.raises(ValidationFailed):
            services.set_message(
                make_campaign(), message_type=CampaignMessageType.TEXT, body_text="  "
            )


class TestValidation:
    def test_ready_campaign_has_no_blockers(self, ready_campaign) -> None:
        assert services.validation_blockers(ready_campaign) == []

    def test_missing_audience_is_a_blocker(self, make_campaign, approved_template) -> None:
        campaign = make_campaign()
        assert any("group" in b.lower() for b in services.validation_blockers(campaign))

    def test_missing_template_is_a_blocker(self, make_campaign, group, make_contact) -> None:
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=True))
        campaign = make_campaign()
        services.set_audience(campaign, [group])

        assert any("template" in b.lower() for b in services.validation_blockers(campaign))

    def test_audience_without_consent_is_a_blocker(
        self, make_campaign, approved_template, group, make_contact
    ) -> None:
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=False))
        campaign = make_campaign()
        services.set_audience(campaign, [group])
        services.set_message(
            campaign,
            message_type=CampaignMessageType.TEMPLATE,
            template=approved_template,
            variable_mapping={
                "name": {"source": "contact_field", "value": "name"},
                "order_id": {"source": "literal", "value": "X"},
            },
        )

        assert any("consent" in b.lower() for b in services.validation_blockers(campaign))

    @override_settings(WHATSAPP_PROVIDER="meta")
    def test_local_template_is_blocked_under_the_live_provider(
        self, make_campaign, local_template, group, make_contact
    ) -> None:
        """The safeguard that stops an unapproved template reaching real recipients."""
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=True))
        campaign = make_campaign()
        services.set_audience(campaign, [group])
        services.set_message(
            campaign,
            message_type=CampaignMessageType.TEMPLATE,
            template=local_template,
            variable_mapping={
                "name": {"source": "contact_field", "value": "name"},
                "offer": {"source": "literal", "value": "20% off"},
            },
        )

        blockers = services.validation_blockers(campaign)
        assert any("WhatsApp Manager" in b for b in blockers)

    @override_settings(CAMPAIGN_MAX_RECIPIENTS=2)
    def test_oversized_audience_is_a_blocker(self, ready_campaign) -> None:
        assert any("limit" in b.lower() for b in services.validation_blockers(ready_campaign))


class TestPreview:
    def test_preview_reports_counts_and_a_sample(self, ready_campaign) -> None:
        preview = services.preview_campaign(ready_campaign)

        assert preview.audience.eligible == 3
        assert preview.is_ready
        assert preview.sample_recipient is not None
        assert "A-100" in preview.sample_text

    def test_preview_uses_example_values_when_nobody_is_eligible(
        self, make_campaign, approved_template, group, make_contact
    ) -> None:
        GroupMembership.objects.create(group=group, contact=make_contact(opted_in=False))
        campaign = make_campaign()
        services.set_audience(campaign, [group])
        services.set_message(
            campaign,
            message_type=CampaignMessageType.TEMPLATE,
            template=approved_template,
            variable_mapping={
                "name": {"source": "contact_field", "value": "name"},
                "order_id": {"source": "literal", "value": "A-1"},
            },
        )

        preview = services.preview_campaign(campaign)

        assert preview.sample_recipient is None
        assert preview.sample_text
        assert not preview.is_ready


class TestStateMachine:
    def test_draft_can_become_processing(self, make_campaign) -> None:
        campaign = make_campaign()
        services.transition(campaign, CampaignStatus.PROCESSING)
        assert campaign.status == CampaignStatus.PROCESSING

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (CampaignStatus.COMPLETED, CampaignStatus.PROCESSING),
            (CampaignStatus.CANCELLED, CampaignStatus.PROCESSING),
            (CampaignStatus.DRAFT, CampaignStatus.COMPLETED),
            (CampaignStatus.DRAFT, CampaignStatus.PAUSED),
            (CampaignStatus.COMPLETED, CampaignStatus.DRAFT),
        ],
    )
    def test_illegal_transitions_are_refused(
        self, make_campaign, from_status, to_status
    ) -> None:
        """Relaunching a completed campaign would message everyone a second time."""
        campaign = make_campaign(status=from_status)
        with pytest.raises(InvalidStateTransition):
            services.transition(campaign, to_status)

    def test_completed_campaign_cannot_be_launched_again(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        ready_campaign.status = CampaignStatus.COMPLETED
        ready_campaign.save(update_fields=["status"])

        with pytest.raises(InvalidStateTransition):
            services.launch_campaign(ready_campaign)


class TestSendingAvailability:
    def test_launch_without_a_dispatcher_raises(self, ready_campaign) -> None:
        with pytest.raises(SendingUnavailable):
            services.launch_campaign(ready_campaign)

    def test_failed_launch_leaves_the_campaign_untouched(self, ready_campaign) -> None:
        """No campaign should sit in PROCESSING with nothing able to process it."""
        with pytest.raises(SendingUnavailable):
            services.launch_campaign(ready_campaign)

        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.DRAFT
        assert Message.objects.count() == 0

    def test_is_sending_available_reflects_registration(self, recording_dispatcher) -> None:
        assert dispatch.is_sending_available() is True
        dispatch.clear_dispatcher()
        assert dispatch.is_sending_available() is False


class TestLaunch:
    def test_creates_one_message_per_eligible_recipient(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign)

        assert Message.objects.filter(campaign=ready_campaign).count() == 3
        assert ready_campaign.total_recipients == 3

    def test_messages_start_pending_with_rendered_content(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign)

        message = Message.objects.filter(campaign=ready_campaign).first()
        assert message.status == MessageStatus.PENDING
        assert message.to_phone_number == message.contact.phone_number
        assert "A-100" in message.rendered_payload["text"]
        assert message.template_name == "order_ready"

    def test_non_consenting_contacts_get_no_message(
        self, ready_campaign, group, make_contact, recording_dispatcher
    ) -> None:
        excluded = make_contact("No consent", opted_in=False)
        GroupMembership.objects.create(group=group, contact=excluded)

        services.launch_campaign(ready_campaign)

        assert not Message.objects.filter(contact=excluded).exists()

    def test_campaign_moves_to_processing(self, ready_campaign, recording_dispatcher) -> None:
        services.launch_campaign(ready_campaign)

        ready_campaign.refresh_from_db()
        assert ready_campaign.status == CampaignStatus.PROCESSING
        assert ready_campaign.started_at is not None

    def test_dispatcher_receives_the_campaign(
        self, ready_campaign, recording_dispatcher, django_capture_on_commit_callbacks
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            services.launch_campaign(ready_campaign)

        assert recording_dispatcher.calls == [ready_campaign]

    def test_dispatch_happens_only_after_commit(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        """A worker must never see a message row that is not yet committed."""
        services.launch_campaign(ready_campaign)
        assert recording_dispatcher.calls == []

    def test_launch_is_audited_with_the_recipient_count(
        self, ready_campaign, operator, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign, user=operator)

        entry = AuditLog.objects.get(action=AuditAction.CAMPAIGN_LAUNCHED)
        assert entry.user == operator
        assert entry.metadata["recipients"] == 3

    def test_invalid_campaign_is_refused(
        self, make_campaign, recording_dispatcher
    ) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            services.launch_campaign(make_campaign())
        assert exc_info.value.details["blockers"]

    def test_materialization_is_idempotent(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        """unique(campaign, contact) means a retried launch tops up, not duplicates."""
        recipients = list(services.resolve_audience(ready_campaign))

        services.materialize_messages(ready_campaign, recipients)
        total = services.materialize_messages(ready_campaign, recipients)

        assert total == 3
        assert Message.objects.filter(campaign=ready_campaign).count() == 3


class TestLifecycle:
    def test_pause_then_resume(self, ready_campaign, recording_dispatcher) -> None:
        services.launch_campaign(ready_campaign)

        services.pause_campaign(ready_campaign)
        assert ready_campaign.status == CampaignStatus.PAUSED

        services.resume_campaign(ready_campaign)
        assert ready_campaign.status == CampaignStatus.PROCESSING

    def test_pausing_a_draft_is_refused(self, ready_campaign) -> None:
        with pytest.raises(InvalidStateTransition):
            services.pause_campaign(ready_campaign)

    def test_resume_requires_a_dispatcher(self, ready_campaign, recording_dispatcher) -> None:
        services.launch_campaign(ready_campaign)
        services.pause_campaign(ready_campaign)
        dispatch.clear_dispatcher()

        with pytest.raises(SendingUnavailable):
            services.resume_campaign(ready_campaign)

    def test_cancel_abandons_unsent_messages(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign)

        services.cancel_campaign(ready_campaign)

        assert ready_campaign.status == CampaignStatus.CANCELLED
        assert Message.objects.filter(status=MessageStatus.FAILED).count() == 3
        assert Message.objects.first().error_code == "cancelled"

    def test_cancel_does_not_touch_already_sent_messages(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        """Messages the provider already accepted cannot be recalled."""
        services.launch_campaign(ready_campaign)
        sent = Message.objects.filter(campaign=ready_campaign).first()
        sent.status = MessageStatus.SENT
        sent.save(update_fields=["status"])

        services.cancel_campaign(ready_campaign)

        sent.refresh_from_db()
        assert sent.status == MessageStatus.SENT

    def test_cancel_is_audited(self, ready_campaign, operator, recording_dispatcher) -> None:
        services.launch_campaign(ready_campaign, user=operator)
        services.cancel_campaign(ready_campaign, user=operator)

        assert AuditLog.objects.filter(action=AuditAction.CAMPAIGN_CANCELLED).exists()


class TestFinalization:
    def test_completes_when_nothing_is_in_flight(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign)
        Message.objects.filter(campaign=ready_campaign).update(status=MessageStatus.SENT)

        assert services.finalize_if_complete(ready_campaign) is True
        assert ready_campaign.status == CampaignStatus.COMPLETED
        assert ready_campaign.completed_at is not None

    def test_does_not_complete_while_messages_remain(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign)

        assert services.finalize_if_complete(ready_campaign) is False
        assert ready_campaign.status == CampaignStatus.PROCESSING

    def test_failed_messages_still_allow_completion(
        self, ready_campaign, recording_dispatcher
    ) -> None:
        services.launch_campaign(ready_campaign)
        Message.objects.filter(campaign=ready_campaign).update(status=MessageStatus.FAILED)

        assert services.finalize_if_complete(ready_campaign) is True

    def test_a_draft_is_never_finalized(self, make_campaign) -> None:
        assert services.finalize_if_complete(make_campaign()) is False


class TestCreation:
    def test_create_requires_a_name(self, operator, organization) -> None:
        with pytest.raises(ValidationFailed):
            services.create_campaign(name="   ", user=operator, organization=organization)

    def test_creation_is_audited(self, operator, organization) -> None:
        campaign = services.create_campaign(name="Launch", user=operator, organization=organization)

        entry = AuditLog.objects.get(action=AuditAction.CAMPAIGN_CREATED)
        assert entry.object_id == str(campaign.pk)

    def test_new_campaigns_start_as_drafts(self, operator, organization) -> None:
        campaign = services.create_campaign(name="Launch", user=operator, organization=organization)
        assert campaign.status == CampaignStatus.DRAFT
        assert Campaign.objects.editable().count() == 1
