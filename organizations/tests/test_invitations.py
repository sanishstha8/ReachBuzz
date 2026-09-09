"""
Bringing a colleague into an organization.

The properties worth protecting: an invitation offers a seat but creates
nothing until somebody proves they hold the address; a seat is checked at
send and re-checked at accept, because those are two different moments and
either one can be the one that is wrong; ownership is never sent by email;
and an organization can never be left with no owner.
"""

from __future__ import annotations

import pytest
from django.core import mail

from core.exceptions import ConflictError, NotAllowed, ValidationFailed
from core.models import AuditAction, AuditLog
from organizations import invitations
from organizations.models import (
    Invitation,
    InvitationStatus,
    OrganizationMember,
    OrganizationRole,
)

pytestmark = pytest.mark.django_db


def invitation_link(body: str) -> str:
    for word in body.split():
        if "/organization/invitations/" in word:
            return word
    raise AssertionError(f"no invitation link in:\n{body}")


class TestSendingAnInvitation:
    def test_it_creates_a_pending_invitation(self, organization, operator) -> None:
        invitation = invitations.invite(
            organization, email="new@example.com", invited_by=operator
        )

        assert invitation.status == InvitationStatus.PENDING
        assert invitation.email == "new@example.com"
        assert invitation.expires_at > invitation.created_at

    def test_the_email_is_lowercased(self, organization) -> None:
        invitation = invitations.invite(organization, email="New@Example.COM")

        assert invitation.email == "new@example.com"

    def test_it_sends_a_link(self, organization) -> None:
        invitations.invite(organization, email="new@example.com")

        assert len(mail.outbox) == 1
        assert invitation_link(mail.outbox[0].body)

    def test_ownership_cannot_be_invited(self, organization) -> None:
        with pytest.raises(ValidationFailed, match="only offer Administrator or Member"):
            invitations.invite(organization, email="new@example.com", role=OrganizationRole.OWNER)

    def test_an_existing_member_cannot_be_invited_again(
        self, organization, operator
    ) -> None:
        with pytest.raises(ConflictError, match="already a member"):
            invitations.invite(organization, email=operator.email)

    def test_the_check_is_case_insensitive(self, organization, operator) -> None:
        with pytest.raises(ConflictError):
            invitations.invite(organization, email=operator.email.upper())

    def test_inviting_twice_resends_rather_than_duplicates(self, organization) -> None:
        first = invitations.invite(organization, email="new@example.com")
        second = invitations.invite(organization, email="new@example.com")

        assert first.pk == second.pk
        assert Invitation.objects.filter(organization=organization).count() == 1
        assert len(mail.outbox) == 2

    def test_resending_pushes_the_expiry_out(self, organization) -> None:
        from django.utils import timezone

        first = invitations.invite(organization, email="new@example.com")
        Invitation.objects.filter(pk=first.pk).update(
            expires_at=timezone.now() - timezone.timedelta(days=1)
        )

        second = invitations.invite(organization, email="new@example.com")

        assert second.expires_at > timezone.now()

    def test_it_is_audited(self, organization, operator) -> None:
        invitations.invite(organization, email="new@example.com", invited_by=operator)

        entry = AuditLog.objects.get(action=AuditAction.MEMBER_INVITED)
        assert entry.user == operator
        assert entry.metadata["email"] == "new@example.com"


class TestTheSeatIsCheckedAtSendTime:
    def test_an_organization_at_its_limit_cannot_invite(
        self, organization, on_plan, make_plan, operator
    ) -> None:
        """One seat already used by the owner; the limit is that one seat."""
        on_plan(organization, make_plan("solo", max_team_members=1))

        with pytest.raises(ValidationFailed, match="team limit"):
            invitations.invite(organization, email="new@example.com")

    def test_a_pending_invitation_counts_against_the_limit(
        self, organization, on_plan, make_plan
    ) -> None:
        """
        Otherwise an organization at its ceiling could send unlimited
        invitations, all of which would then race each other at acceptance.
        """
        on_plan(organization, make_plan("two-seats", max_team_members=2))
        invitations.invite(organization, email="first@example.com")

        with pytest.raises(ValidationFailed):
            invitations.invite(organization, email="second@example.com")

    def test_an_unlimited_plan_never_refuses(
        self, organization, on_plan, make_plan
    ) -> None:
        on_plan(organization, make_plan("boundless", max_team_members=None))

        for n in range(3):
            invitations.invite(organization, email=f"person{n}@example.com")

        assert Invitation.objects.filter(organization=organization).count() == 3


class TestRevoking:
    def test_it_closes_the_invitation(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")

        invitations.revoke(invitation)

        invitation.refresh_from_db()
        assert invitation.status == InvitationStatus.REVOKED
        assert invitation.revoked_at is not None

    def test_a_revoked_link_no_longer_works(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        invitations.revoke(invitation)

        assert invitations.find_open(invitation.token) is None

    def test_revoking_twice_is_a_no_op(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        invitations.revoke(invitation)
        revoked_at = Invitation.objects.get(pk=invitation.pk).revoked_at

        invitations.revoke(invitation)

        assert Invitation.objects.get(pk=invitation.pk).revoked_at == revoked_at

    def test_it_is_audited(self, organization, operator) -> None:
        invitation = invitations.invite(organization, email="new@example.com")

        invitations.revoke(invitation, user=operator)

        assert AuditLog.objects.filter(action=AuditAction.INVITATION_REVOKED).exists()


class TestFindingAnOpenInvitation:
    def test_an_unknown_token_is_none(self) -> None:
        assert invitations.find_open("not-a-real-token") is None

    def test_an_expired_invitation_is_not_open(self, organization) -> None:
        from django.utils import timezone

        invitation = invitations.invite(organization, email="new@example.com")
        Invitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )

        assert invitations.find_open(invitation.token) is None

    def test_an_accepted_invitation_is_not_open(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        invitations.accept(invitation, first_name="A", password="correct-horse-battery-staple")

        assert invitations.find_open(invitation.token) is None


class TestAccepting:
    def test_it_creates_an_account_for_a_stranger(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")

        member = invitations.accept(
            invitation, first_name="New", password="correct-horse-battery-staple"
        )

        assert member.user.email == "new@example.com"
        assert member.organization == organization

    def test_the_new_account_is_verified_immediately(self, organization) -> None:
        """
        Following the mailed link is the same proof of holding the address
        that Stage 2's confirmation link is built on.
        """
        invitation = invitations.invite(organization, email="new@example.com")

        member = invitations.accept(
            invitation, first_name="New", password="correct-horse-battery-staple"
        )

        assert member.user.email_verified is True
        assert member.user.email_verified_at is not None

    def test_it_uses_the_role_the_invitation_offered(self, organization) -> None:
        invitation = invitations.invite(
            organization, email="new@example.com", role=OrganizationRole.ADMIN
        )

        member = invitations.accept(
            invitation, first_name="New", password="correct-horse-battery-staple"
        )

        assert member.role == OrganizationRole.ADMIN

    def test_an_existing_user_can_accept_directly(
        self, organization, other_organization
    ) -> None:
        """Somebody who already has an account just gets a second seat."""
        outsider = other_organization.owner
        invitation = invitations.invite(organization, email=outsider.email)

        member = invitations.accept(invitation, user=outsider)

        assert member.user == outsider
        assert OrganizationMember.objects.filter(
            organization=organization, user=outsider
        ).exists()

    def test_the_wrong_signed_in_user_is_refused(
        self, organization, other_organization
    ) -> None:
        invitation = invitations.invite(organization, email="new@example.com")

        with pytest.raises(NotAllowed, match="different address"):
            invitations.accept(invitation, user=other_organization.owner)

    def test_a_closed_invitation_cannot_be_accepted(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        invitations.revoke(invitation)

        with pytest.raises(ValidationFailed, match="no longer open"):
            invitations.accept(
                invitation, first_name="New", password="correct-horse-battery-staple"
            )

    def test_accepting_twice_is_refused(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        invitations.accept(invitation, first_name="New", password="correct-horse-battery-staple")

        with pytest.raises(ValidationFailed):
            invitations.accept(
                invitation, first_name="New", password="correct-horse-battery-staple"
            )

    def test_it_is_audited(self, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")

        invitations.accept(invitation, first_name="New", password="correct-horse-battery-staple")

        assert AuditLog.objects.filter(action=AuditAction.INVITATION_ACCEPTED).exists()


class TestTheSeatIsCheckedAgainAtAcceptTime:
    def test_a_seat_that_filled_up_in_the_meantime_is_refused(
        self, organization, on_plan, make_plan, make_user
    ) -> None:
        """
        A plan can change, or another invitation can be accepted, in the time
        between sending this one and it being opened. Accepting is the moment
        a seat is actually spent, so that is the check that has to be right.
        """
        plan = make_plan("two-seats", max_team_members=2)
        on_plan(organization, plan)
        invitation = invitations.invite(organization, email="new@example.com")

        # A second member joins in the meantime, filling the plan.
        OrganizationMember.objects.create(
            organization=organization,
            user=make_user("filled-the-seat@example.com"),
            role=OrganizationRole.MEMBER,
        )

        with pytest.raises(ValidationFailed):
            invitations.accept(
                invitation, first_name="New", password="correct-horse-battery-staple"
            )


class TestRemovingAMember:
    def test_it_removes_the_seat(self, organization, make_user) -> None:
        colleague = OrganizationMember.objects.create(
            organization=organization,
            user=make_user("c@example.com"),
            role=OrganizationRole.MEMBER,
        )

        invitations.remove_member(colleague)

        assert not OrganizationMember.objects.filter(pk=colleague.pk).exists()

    def test_the_last_owner_cannot_be_removed(self, organization) -> None:
        owner_membership = OrganizationMember.objects.get(
            organization=organization, role=OrganizationRole.OWNER
        )

        with pytest.raises(ValidationFailed, match="at least one owner"):
            invitations.remove_member(owner_membership)

    def test_an_owner_can_be_removed_if_another_remains(
        self, organization, make_user
    ) -> None:
        second_owner = OrganizationMember.objects.create(
            organization=organization,
            user=make_user("co-owner@example.com"),
            role=OrganizationRole.OWNER,
        )
        first_owner = OrganizationMember.objects.get(
            organization=organization, user=organization.owner
        )

        invitations.remove_member(first_owner)

        assert not OrganizationMember.objects.filter(pk=first_owner.pk).exists()
        assert OrganizationMember.objects.filter(pk=second_owner.pk).exists()

    def test_it_is_audited(self, organization, make_user) -> None:
        colleague = OrganizationMember.objects.create(
            organization=organization, user=make_user("c2@example.com"), role=OrganizationRole.MEMBER
        )

        invitations.remove_member(colleague)

        assert AuditLog.objects.filter(action=AuditAction.MEMBER_REMOVED).exists()
