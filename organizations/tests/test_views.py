"""
The members page and the accept-invitation flow, at the HTTP layer.

Most of what matters here is access: any member can read the team list, only
an owner or administrator can change it, and the accept flow is the one public
page in this app — reachable by someone who is not signed in at all.
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse

from organizations import invitations
from organizations.models import Invitation, OrganizationMember, OrganizationRole

pytestmark = pytest.mark.django_db


def demote(organization, user, role=OrganizationRole.MEMBER):
    OrganizationMember.objects.filter(organization=organization, user=user).update(role=role)


def invitation_link(body: str) -> str:
    for word in body.split():
        if "/organization/invitations/" in word:
            return word
    raise AssertionError(f"no invitation link in:\n{body}")


class TestWhoCanSeeTheTeam:
    def test_an_anonymous_visitor_is_sent_to_sign_in(self, client) -> None:
        response = client.get(reverse("organizations:members"))

        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_an_ordinary_member_can_read_the_list(
        self, auth_client, organization, operator
    ) -> None:
        demote(organization, operator)

        response = auth_client.get(reverse("organizations:members"))

        assert response.status_code == 200
        assert response.context["can_manage_members"] is False

    def test_an_owner_can_manage(self, auth_client) -> None:
        response = auth_client.get(reverse("organizations:members"))

        assert response.context["can_manage_members"] is True

    def test_an_administrator_can_too(self, auth_client, organization, operator) -> None:
        demote(organization, operator, OrganizationRole.ADMIN)

        assert auth_client.get(
            reverse("organizations:members")
        ).context["can_manage_members"] is True

    def test_the_page_lists_current_members(self, auth_client, organization) -> None:
        body = auth_client.get(reverse("organizations:members")).content.decode()

        assert organization.owner.email in body


class TestSendingAnInvitationNeedsARole:
    def test_a_member_cannot_invite(self, auth_client, organization, operator) -> None:
        demote(organization, operator)

        auth_client.post(
            reverse("organizations:invite-member"), {"email": "new@example.com", "role": "member"}
        )

        assert not Invitation.objects.filter(organization=organization).exists()

    def test_an_owner_can(self, auth_client, organization) -> None:
        auth_client.post(
            reverse("organizations:invite-member"), {"email": "new@example.com", "role": "member"}
        )

        assert Invitation.objects.filter(organization=organization, email="new@example.com").exists()

    def test_a_get_does_not_send_anything(self, auth_client, organization) -> None:
        """A GET that mails somebody can be fired by a link prefetcher."""
        response = auth_client.get(reverse("organizations:invite-member"))

        assert response.status_code == 405
        assert not Invitation.objects.filter(organization=organization).exists()

    def test_ownership_is_refused_at_the_form_too(self, auth_client) -> None:
        auth_client.post(
            reverse("organizations:invite-member"),
            {"email": "new@example.com", "role": "owner"},
        )

        assert not Invitation.objects.filter(email="new@example.com").exists()

    def test_a_refusal_shows_the_reason(self, auth_client, operator) -> None:
        response = auth_client.post(
            reverse("organizations:invite-member"),
            {"email": operator.email, "role": "member"},
            follow=True,
        )

        assert "already a member" in response.content.decode()


class TestRevokeAndResend:
    def test_an_owner_can_revoke(self, auth_client, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")

        auth_client.post(reverse("organizations:revoke-invitation", args=[invitation.pk]))

        invitation.refresh_from_db()
        assert invitation.status == "revoked"

    def test_a_member_cannot_revoke(self, auth_client, organization, operator) -> None:
        demote(organization, operator)
        invitation = invitations.invite(organization, email="new@example.com")

        auth_client.post(reverse("organizations:revoke-invitation", args=[invitation.pk]))

        invitation.refresh_from_db()
        assert invitation.status == "pending"

    def test_resending_sends_another_email(self, auth_client, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        mail.outbox.clear()

        auth_client.post(reverse("organizations:resend-invitation", args=[invitation.pk]))

        assert len(mail.outbox) == 1

    def test_another_organizations_invitation_is_a_404(
        self, auth_client, other_organization
    ) -> None:
        """Not scoped by role alone — by tenant, the same as everything else."""
        theirs = invitations.invite(other_organization, email="new@example.com")

        response = auth_client.post(
            reverse("organizations:revoke-invitation", args=[theirs.pk])
        )

        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.status == "pending"


class TestRemovingAMember:
    def test_an_owner_can_remove_a_member(self, auth_client, organization, make_user) -> None:
        colleague = OrganizationMember.objects.create(
            organization=organization, user=make_user("c@example.com"), role=OrganizationRole.MEMBER
        )

        auth_client.post(reverse("organizations:remove-member", args=[colleague.pk]))

        assert not OrganizationMember.objects.filter(pk=colleague.pk).exists()

    def test_a_member_cannot_remove_anybody(
        self, auth_client, organization, operator, make_user
    ) -> None:
        demote(organization, operator)
        colleague = OrganizationMember.objects.create(
            organization=organization, user=make_user("c@example.com"), role=OrganizationRole.MEMBER
        )

        auth_client.post(reverse("organizations:remove-member", args=[colleague.pk]))

        assert OrganizationMember.objects.filter(pk=colleague.pk).exists()

    def test_the_last_owner_cannot_be_removed_via_the_view(
        self, auth_client, organization
    ) -> None:
        owner_membership = OrganizationMember.objects.get(
            organization=organization, role=OrganizationRole.OWNER
        )

        response = auth_client.post(
            reverse("organizations:remove-member", args=[owner_membership.pk]), follow=True
        )

        assert OrganizationMember.objects.filter(pk=owner_membership.pk).exists()
        assert "at least one owner" in response.content.decode()


class TestAcceptingIsPublic:
    def test_an_anonymous_visitor_can_open_it(self, client, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")

        response = client.get(
            reverse("organizations:invitation-accept", args=[invitation.token])
        )

        assert response.status_code == 200
        assert "new@example.com" in response.content.decode()

    def test_an_unknown_token_is_a_404(self, client) -> None:
        response = client.get(
            reverse("organizations:invitation-accept", args=["not-a-real-token"])
        )

        assert response.status_code == 404

    def test_a_revoked_invitation_is_a_404(self, client, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        invitations.revoke(invitation)

        response = client.get(
            reverse("organizations:invitation-accept", args=[invitation.token])
        )

        assert response.status_code == 404

    def test_a_stranger_can_create_an_account_and_accept(
        self, client, organization
    ) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        url = reverse("organizations:invitation-accept", args=[invitation.token])

        response = client.post(
            url,
            {
                "first_name": "New",
                "last_name": "Person",
                "password1": "correct-horse-battery-staple",
                "password2": "correct-horse-battery-staple",
            },
        )

        assert response.status_code == 302
        assert OrganizationMember.objects.filter(
            organization=organization, user__email="new@example.com"
        ).exists()

    def test_accepting_signs_the_new_person_in(self, client, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        url = reverse("organizations:invitation-accept", args=[invitation.token])

        client.post(
            url,
            {
                "first_name": "New",
                "last_name": "",
                "password1": "correct-horse-battery-staple",
                "password2": "correct-horse-battery-staple",
            },
        )

        assert "_auth_user_id" in client.session

    def test_a_mismatched_password_re_renders_with_an_error(
        self, client, organization
    ) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        url = reverse("organizations:invitation-accept", args=[invitation.token])

        response = client.post(
            url,
            {
                "first_name": "New",
                "password1": "correct-horse-battery-staple",
                "password2": "does-not-match",
            },
        )

        assert response.status_code == 200
        assert not OrganizationMember.objects.filter(
            organization=organization, user__email="new@example.com"
        ).exists()

    def test_an_existing_user_signed_in_as_themselves_accepts_with_one_click(
        self, organization, other_organization
    ) -> None:
        from django.test import Client

        outsider = other_organization.owner
        invitation = invitations.invite(organization, email=outsider.email)
        url = reverse("organizations:invitation-accept", args=[invitation.token])

        client = Client()
        client.force_login(outsider)
        response = client.post(url)

        assert response.status_code == 302
        assert OrganizationMember.objects.filter(
            organization=organization, user=outsider
        ).exists()

    def test_a_different_signed_in_user_is_told_to_sign_out(
        self, auth_client, organization, other_organization
    ) -> None:
        """auth_client is signed in as the *other* organization's owner."""
        invitation = invitations.invite(other_organization, email="new-elsewhere@example.com")
        url = reverse("organizations:invitation-accept", args=[invitation.token])

        response = auth_client.post(url, follow=True)

        assert "Sign out" in response.content.decode() or "sign out" in response.content.decode().lower()
        assert not OrganizationMember.objects.filter(organization=other_organization, user__email="new-elsewhere@example.com").exists()

    def test_the_link_stops_working_once_used(self, client, organization) -> None:
        invitation = invitations.invite(organization, email="new@example.com")
        url = reverse("organizations:invitation-accept", args=[invitation.token])
        client.post(
            url,
            {
                "first_name": "New",
                "password1": "correct-horse-battery-staple",
                "password2": "correct-horse-battery-staple",
            },
        )

        response = client.get(url)

        assert response.status_code == 404


class TestTheNavShowsTeamToEverySignedInUser:
    def test_it_is_present(self, auth_client) -> None:
        body = auth_client.get(reverse("dashboard:home")).content.decode()

        assert reverse("organizations:members") in body


class TestCrossTenantIsolation:
    """
    The accept page is the one route in this app anyone can reach unauthenticated,
    which makes it the highest-risk surface here. Both directions are checked:
    an admin cannot manage another tenant's invitations, and accepting one
    invitation never grants a seat in the wrong organization.
    """

    def test_accepting_only_ever_adds_the_invited_organization(
        self, client, organization, other_organization
    ) -> None:
        invitation = invitations.invite(organization, email="cross-tenant@example.com")
        url = reverse("organizations:invitation-accept", args=[invitation.token])

        client.post(
            url,
            {
                "first_name": "Cross",
                "password1": "correct-horse-battery-staple",
                "password2": "correct-horse-battery-staple",
            },
        )

        assert OrganizationMember.objects.filter(
            organization=organization, user__email="cross-tenant@example.com"
        ).exists()
        assert not OrganizationMember.objects.filter(
            organization=other_organization, user__email="cross-tenant@example.com"
        ).exists()

    def test_the_member_list_never_shows_another_tenants_seats(
        self, auth_client, organization, other_organization, make_user
    ) -> None:
        OrganizationMember.objects.create(
            organization=other_organization,
            user=make_user("theirs@example.com"),
            role=OrganizationRole.MEMBER,
        )

        body = auth_client.get(reverse("organizations:members")).content.decode()

        assert "theirs@example.com" not in body
