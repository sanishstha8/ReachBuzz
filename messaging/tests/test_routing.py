"""
Sending over two channels.

The seam where the two unlike providers meet. What is being checked is that the
channel decision is made once, from the message, and that everything downstream
of it — retries, claims, the rate limiter, the status machine — did not have to
learn there is more than one channel.

And the safety property that matters more than any of it: an SMS campaign
reaches only the people who agreed to SMS.
"""

from __future__ import annotations

import pytest

from campaigns.models import CampaignMessageType, CampaignStatus
from contacts.models import ContactGroup, GroupMembership
from contacts.services import set_consent
from core.channels import Channel
from messaging import routing
from messaging.models import Message

pytestmark = pytest.mark.django_db


@pytest.fixture
def group(organization):
    return ContactGroup.objects.create(name="Everyone", organization=organization)


@pytest.fixture
def mixed_audience(organization, group, make_contact):
    """
    Four people who have each agreed to something different.

    The point of the fixture: no campaign on either channel should reach all
    four, and which three it misses differs by channel.
    """
    whatsapp_only = make_contact("WhatsApp only", "+9779800000001", opted_in=True)

    sms_only = make_contact("SMS only", "+9779800000002", opted_in=False)
    set_consent(sms_only, opted_in=True, channel=Channel.SMS)

    both = make_contact("Both", "+9779800000003", opted_in=True)
    set_consent(both, opted_in=True, channel=Channel.SMS)

    neither = make_contact("Neither", "+9779800000004", opted_in=False)

    for contact in (whatsapp_only, sms_only, both, neither):
        GroupMembership.objects.create(group=group, contact=contact)

    return {"whatsapp_only": whatsapp_only, "sms_only": sms_only, "both": both, "neither": neither}


def build(make_campaign, group, channel, **extra):
    from campaigns.services import set_audience, set_message

    campaign = make_campaign(f"{channel} campaign", channel=channel)
    set_audience(campaign, [group])
    set_message(
        campaign,
        message_type=CampaignMessageType.TEXT,
        body_text=extra.get("body", "Your order is ready."),
    )
    return campaign


class TestAudienceRespectsTheChannel:
    def test_an_sms_campaign_reaches_only_sms_consent(
        self, make_campaign, group, mixed_audience
    ) -> None:
        """
        The failure this whole stage exists to prevent. Two of these four people
        agreed to WhatsApp and would have been texted by a naive implementation.
        """
        from campaigns.services import resolve_audience

        campaign = build(make_campaign, group, Channel.SMS)

        reached = set(resolve_audience(campaign))

        assert reached == {mixed_audience["sms_only"], mixed_audience["both"]}
        assert mixed_audience["whatsapp_only"] not in reached

    def test_a_whatsapp_campaign_reaches_only_whatsapp_consent(
        self, make_campaign, group, mixed_audience
    ) -> None:
        from campaigns.services import resolve_audience

        campaign = build(make_campaign, group, Channel.WHATSAPP)

        reached = set(resolve_audience(campaign))

        assert reached == {mixed_audience["whatsapp_only"], mixed_audience["both"]}
        assert mixed_audience["sms_only"] not in reached

    def test_neither_channel_reaches_somebody_who_agreed_to_nothing(
        self, make_campaign, group, mixed_audience
    ) -> None:
        from campaigns.services import resolve_audience

        for channel in (Channel.WHATSAPP, Channel.SMS):
            campaign = build(make_campaign, group, channel)
            assert mixed_audience["neither"] not in set(resolve_audience(campaign))


class TestLaunchingAnSmsCampaign:
    def test_the_messages_carry_the_channel(
        self, make_campaign, group, mixed_audience, recording_dispatcher, organization
    ) -> None:
        from campaigns.services import launch_campaign

        campaign = build(make_campaign, group, Channel.SMS)

        launch_campaign(campaign, user=organization.owner)

        messages = Message.objects.filter(campaign=campaign)
        assert messages.count() == 2
        assert all(message.channel == Channel.SMS for message in messages)

    def test_a_template_campaign_cannot_go_by_sms(
        self, make_campaign, group, mixed_audience, approved_template, organization
    ) -> None:
        """
        Refused at validation, not discovered a thousand messages in. SMS has no
        approved-template registry to select from.
        """
        from campaigns.services import set_audience, set_message, validation_blockers

        campaign = make_campaign("Template over SMS", channel=Channel.SMS)
        set_audience(campaign, [group])
        set_message(
            campaign,
            message_type=CampaignMessageType.TEMPLATE,
            template=approved_template,
            variable_mapping={
                "name": {"source": "contact_field", "value": "name"},
                "order_id": {"source": "literal", "value": "A-1"},
            },
        )

        blockers = validation_blockers(campaign)

        assert any("SMS has no approved templates" in blocker for blocker in blockers)

    def test_an_empty_sms_audience_names_the_channel(
        self, make_campaign, group, make_contact
    ) -> None:
        """
        "Nobody has consented" is confusing to somebody looking at a group full
        of opted-in WhatsApp contacts. The message says which channel it means.
        """
        from campaigns.services import validation_blockers

        contact = make_contact("WhatsApp only", "+9779800000021", opted_in=True)
        GroupMembership.objects.create(group=group, contact=contact)
        campaign = build(make_campaign, group, Channel.SMS)

        blockers = validation_blockers(campaign)

        assert any("consent for SMS" in blocker for blocker in blockers)

    def test_the_campaign_reaches_processing(
        self, make_campaign, group, mixed_audience, recording_dispatcher, organization
    ) -> None:
        """The rest of the launch path did not have to learn about channels."""
        from campaigns.services import launch_campaign

        campaign = build(make_campaign, group, Channel.SMS)

        launch_campaign(campaign, user=organization.owner)

        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PROCESSING


class TestTheRouter:
    def test_it_picks_the_sms_gateway_for_an_sms_message(self, organization) -> None:
        from sms.providers.mock_provider import MockSmsProvider

        provider = routing.sender_for(organization, Channel.SMS)

        assert isinstance(provider, MockSmsProvider)

    def test_it_picks_a_whatsapp_provider_otherwise(self, organization) -> None:
        from whatsapp.services.base import WhatsAppProvider

        provider = routing.sender_for(organization, Channel.WHATSAPP)

        assert isinstance(provider, WhatsAppProvider)

    def test_sending_an_sms_message_goes_through_the_gateway(
        self, organization, make_campaign, make_contact, group
    ) -> None:
        campaign = make_campaign("Texts", channel=Channel.SMS)
        contact = make_contact("Reader", "+9779800000031")
        message = Message.objects.create(
            campaign=campaign,
            contact=contact,
            to_phone_number=contact.phone_number,
            message_type=CampaignMessageType.TEXT,
            rendered_payload={"text": "Your order is ready."},
        )

        result = routing.send(message)

        assert result.success
        assert result.provider_message_id.startswith("mock_sms_")

    def test_a_template_message_on_sms_fails_permanently(
        self, organization, make_campaign, make_contact
    ) -> None:
        """Retrying it a thousand times will not make SMS grow a template registry."""
        campaign = make_campaign("Wrong", channel=Channel.SMS)
        contact = make_contact("Reader", "+9779800000032")
        message = Message.objects.create(
            campaign=campaign,
            contact=contact,
            to_phone_number=contact.phone_number,
            message_type=CampaignMessageType.TEMPLATE,
            rendered_payload={},
        )

        result = routing.send(message)

        assert not result.success
        assert result.retryable is False
        assert result.error_code == "template_on_sms"

    def test_preflight_refuses_an_unknown_provider(self, organization, settings) -> None:
        from core.exceptions import ProviderNotConfigured

        settings.SMS_PROVIDER = "carrier-pigeon"

        with pytest.raises(ProviderNotConfigured):
            routing.preflight(organization, Channel.SMS)


class TestTheMessageInheritsItsChannel:
    def test_saving_derives_it_from_the_campaign(
        self, organization, make_campaign, make_contact
    ) -> None:
        """
        A message on a different channel from its campaign would go to somebody
        who consented to the campaign's channel and not to this one.
        """
        campaign = make_campaign("Texts", channel=Channel.SMS)
        contact = make_contact("Reader", "+9779800000041")

        message = Message.objects.create(
            campaign=campaign, contact=contact, to_phone_number=contact.phone_number
        )

        assert message.channel == Channel.SMS

    def test_bulk_created_messages_get_it_too(
        self, make_campaign, group, mixed_audience, recording_dispatcher, organization
    ) -> None:
        """bulk_create does not call save(), so materialization sets it itself."""
        from campaigns.services import launch_campaign

        campaign = build(make_campaign, group, Channel.SMS)
        launch_campaign(campaign, user=organization.owner)

        assert not Message.objects.filter(campaign=campaign).exclude(
            channel=Channel.SMS
        ).exists()

    def test_an_existing_campaign_defaults_to_whatsapp(self, make_campaign) -> None:
        """Every campaign that existed before channels did means WhatsApp."""
        assert make_campaign("Legacy").channel == Channel.WHATSAPP
