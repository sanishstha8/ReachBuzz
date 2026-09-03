"""
Tenant isolation.

The failure this guards against is silent. A query missing its organization
filter does not raise, does not slow down, and does not look wrong — it just
returns another customer's contacts. So these tests assert the *absence* of
data rather than its presence, which is the harder and more important half.

Every owned model is covered by the same parametrised test rather than a
hand-written one each, so a model added later without a scope shows up here
instead of in somebody's inbox.
"""

from __future__ import annotations

import pytest

from campaigns.models import Campaign
from contacts.models import Contact, ContactGroup, ContactImport
from messaging.models import Message
from organizations.models import (
    Organization,
    OrganizationMember,
    OrganizationRole,
    OrganizationStatus,
)
from organizations.scoping import OrganizationOwnedModel, organization_for
from whatsapp.models import MessageTemplate

pytestmark = pytest.mark.django_db

# Everything a customer owns. Adding a model here without scoping it fails.
OWNED_MODELS = [Contact, ContactGroup, ContactImport, Campaign, MessageTemplate, Message]


class TestEveryOwnedModelIsScoped:
    @pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda m: m.__name__)
    def test_it_inherits_the_owned_base(self, model) -> None:
        assert issubclass(model, OrganizationOwnedModel), (
            f"{model.__name__} holds customer data but is not organization-owned"
        )

    @pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda m: m.__name__)
    def test_its_manager_can_scope(self, model) -> None:
        """A model whose manager lacks this cannot be filtered safely."""
        assert hasattr(model.objects, "for_organization"), model.__name__

    @pytest.mark.parametrize("model", OWNED_MODELS, ids=lambda m: m.__name__)
    def test_an_unresolved_organization_returns_nothing(self, model) -> None:
        """
        The critical default. If `for_organization(None)` returned everything,
        a single unresolved request would expose the whole database.
        """
        assert model.objects.for_organization(None).count() == 0


class TestDataDoesNotCross:
    def test_contacts_are_invisible_to_another_organization(
        self, organization, other_organization, make_contact
    ) -> None:
        mine = make_contact("Mine", "+9779800000001", opted_in=True)
        theirs = Contact.objects.create(
            name="Theirs", phone_number="+9779800000002", organization=other_organization
        )

        visible = set(Contact.objects.for_organization(organization).values_list("pk", flat=True))

        assert mine.pk in visible
        assert theirs.pk not in visible

    def test_campaigns_are_invisible_to_another_organization(
        self, organization, other_organization, make_campaign
    ) -> None:
        mine = make_campaign("Mine")
        theirs = Campaign.objects.create(name="Theirs", organization=other_organization)

        visible = Campaign.objects.for_organization(organization)

        assert mine in visible
        assert theirs not in visible

    def test_messages_follow_the_campaign_that_made_them(
        self, organization, other_organization, make_campaign, make_contact, approved_template
    ) -> None:
        """
        A message's organization is derived from its campaign, so the two can
        never disagree — the alternative is a message visible to a customer
        whose campaign is not.
        """
        from campaigns.services import materialize_messages

        campaign = make_campaign("Ours", template=approved_template)
        materialize_messages(campaign, [make_contact("A", opted_in=True)])

        message = Message.objects.get(campaign=campaign)

        assert message.organization_id == organization.pk
        assert Message.objects.for_organization(other_organization).count() == 0

    def test_a_scoped_lookup_by_id_finds_nothing_across_tenants(
        self, organization, other_organization
    ) -> None:
        """
        The shape the whole mechanism exists to prevent: knowing an id is not
        the same as being allowed to read it.
        """
        theirs = Contact.objects.create(
            name="Theirs", phone_number="+9779800000009", organization=other_organization
        )

        assert not Contact.objects.for_organization(organization).filter(pk=theirs.pk).exists()
        # ...while the unscoped manager would happily hand it over.
        assert Contact.objects.filter(pk=theirs.pk).exists()

    def test_for_user_spans_only_the_users_own_organizations(
        self, organization, other_organization, operator, make_contact
    ) -> None:
        mine = make_contact("Mine", "+9779800000003", opted_in=True)
        Contact.objects.create(
            name="Theirs", phone_number="+9779800000004", organization=other_organization
        )

        assert list(Contact.objects.for_user(operator)) == [mine]


class TestOrganizationModel:
    def test_a_slug_is_generated_from_the_name(self, operator) -> None:
        org = Organization.objects.create(name="Acme Trading Pvt Ltd", owner=operator)
        assert org.slug == "acme-trading-pvt-ltd"

    def test_two_businesses_may_share_a_name(self, operator) -> None:
        """Real businesses do. The slug has to resolve rather than raise."""
        first = Organization.objects.create(name="Acme", owner=operator)
        second = Organization.objects.create(name="Acme", owner=operator)

        assert first.slug == "acme"
        assert second.slug == "acme-2"

    def test_a_user_cannot_hold_two_seats_in_one_organization(
        self, organization, operator
    ) -> None:
        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            OrganizationMember.objects.create(organization=organization, user=operator)

    def test_membership_reports_authority(self, organization, operator, make_user) -> None:
        owner = organization.member_for(operator)
        member = OrganizationMember.objects.create(
            organization=organization, user=make_user("m@example.com"), role=OrganizationRole.MEMBER
        )

        assert owner.is_owner and owner.can_administer
        assert not member.is_owner and not member.can_administer


class TestResolvingTheOrganization:
    def test_a_member_resolves_to_their_organization(self, organization, operator) -> None:
        assert organization_for(operator) == organization

    def test_a_user_with_no_membership_resolves_to_nothing(self, make_user) -> None:
        assert organization_for(make_user("nobody@example.com")) is None

    def test_an_anonymous_visitor_resolves_to_nothing(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        assert organization_for(AnonymousUser()) is None

    def test_a_suspended_organization_does_not_resolve(self, organization, operator) -> None:
        """
        Suspension has to actually stop access, not merely label it. Resolving
        to None means every scoped query returns empty.
        """
        organization.status = OrganizationStatus.SUSPENDED
        organization.save(update_fields=["status"])

        assert organization_for(operator) is None
