"""
Self-service sign-up, email confirmation and password reset.

Registration is the one place a stranger can create state in this system, so
most of what matters here is what it refuses: duplicate addresses, weak
passwords, and — the one with teeth — sending campaigns from an address nobody
has confirmed can receive mail.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.urls import reverse

from core.models import AuditAction, AuditLog
from organizations.models import Organization, OrganizationMember, OrganizationRole

pytestmark = pytest.mark.django_db

User = get_user_model()

VALID = {
    "organization_name": "Himalaya Traders",
    "first_name": "Anisha",
    "last_name": "Shrestha",
    "email": "anisha@example.com",
    "phone": "",
    "password1": "correct-horse-battery-staple",
    "password2": "correct-horse-battery-staple",
}


def register(client: Client, **overrides):
    return client.post(reverse("accounts:register"), {**VALID, **overrides})


def _first_path(body: str, prefix: str) -> str:
    for word in body.split():
        index = word.find(prefix)
        if index != -1:
            return word[index:]
    raise AssertionError(f"no {prefix} link in:\n{body}")


def verification_link(body: str) -> str:
    return _first_path(body, "/accounts/verify/")


def reset_link(body: str) -> str:
    return _first_path(body, "/accounts/password/reset/")


class TestRegistration:
    def test_it_creates_the_account_the_business_and_the_ownership(
        self, client: Client
    ) -> None:
        """All three, or none — a customer missing any one of them is broken."""
        register(client)

        user = User.objects.get(email="anisha@example.com")
        organization = Organization.objects.get(name="Himalaya Traders")
        membership = OrganizationMember.objects.get(user=user)

        assert organization.owner == user
        assert membership.organization == organization
        assert membership.role == OrganizationRole.OWNER

    def test_nothing_is_created_when_the_form_is_rejected(self, client: Client) -> None:
        register(client, password2="does-not-match")

        assert not User.objects.filter(email="anisha@example.com").exists()
        assert not Organization.objects.filter(name="Himalaya Traders").exists()

    def test_the_password_is_hashed(self, client: Client) -> None:
        register(client)

        user = User.objects.get(email="anisha@example.com")
        assert user.password != VALID["password1"]
        assert user.check_password(VALID["password1"])

    def test_the_new_account_starts_unverified(self, client: Client) -> None:
        register(client)
        assert User.objects.get(email="anisha@example.com").email_verified is False

    def test_it_signs_the_new_customer_in(self, client: Client) -> None:
        """Making somebody sign in again immediately is friction for nothing."""
        response = register(client)

        assert response.status_code == 302
        assert "_auth_user_id" in client.session

    def test_a_duplicate_address_is_refused(self, client: Client, operator) -> None:
        response = register(client, email=operator.email)

        assert response.status_code == 200
        assert "already uses this email" in response.content.decode()
        assert User.objects.filter(email=operator.email).count() == 1

    def test_the_address_is_matched_case_insensitively(self, client: Client, operator) -> None:
        """Addresses are stored lowercase, so the check has to be too."""
        response = register(client, email=operator.email.upper())

        assert response.status_code == 200
        assert "already uses this email" in response.content.decode()

    def test_a_weak_password_is_refused(self, client: Client) -> None:
        response = register(client, password1="password", password2="password")

        assert response.status_code == 200
        assert not User.objects.filter(email="anisha@example.com").exists()

    def test_registering_is_audited(self, client: Client) -> None:
        register(client)
        assert AuditLog.objects.filter(action=AuditAction.USER_REGISTERED).count() == 1

    def test_a_signed_in_visitor_is_sent_to_their_dashboard(self, auth_client: Client) -> None:
        response = auth_client.get(reverse("accounts:register"))

        assert response.status_code == 302
        assert response.url == reverse("dashboard:home")

    def test_a_phone_number_is_normalised(self, client: Client) -> None:
        register(client, phone="9800000001")
        assert User.objects.get(email="anisha@example.com").phone.startswith("+977")


class TestEmailVerification:
    def test_registering_sends_a_confirmation_link(self, client: Client) -> None:
        register(client)

        assert len(mail.outbox) == 1
        assert "anisha@example.com" in mail.outbox[0].to

    def test_the_link_confirms_the_address(self, client: Client) -> None:
        register(client)

        client.get(verification_link(mail.outbox[0].body))

        user = User.objects.get(email="anisha@example.com")
        assert user.email_verified is True
        assert user.email_verified_at is not None

    def test_the_link_stops_working_once_used(self, client: Client) -> None:
        """The token hashes email_verified, so confirming invalidates it."""
        register(client)
        link = verification_link(mail.outbox[0].body)
        client.get(link)

        response = client.get(link, follow=True)

        assert "invalid or has already been used" in response.content.decode()

    def test_a_tampered_token_is_refused(self, client: Client) -> None:
        register(client)
        link = verification_link(mail.outbox[0].body)

        response = client.get(link[:-4] + "aaa/", follow=True)

        assert "invalid or has already been used" in response.content.decode()
        assert User.objects.get(email="anisha@example.com").email_verified is False

    def test_an_unknown_user_gives_the_same_message(self, client: Client) -> None:
        """
        One message for every failure. Distinguishing "no such account" from
        "bad token" would tell a stranger which addresses are registered.
        """
        response = client.get(
            reverse("accounts:verify-email", kwargs={"uidb64": "bm9wZQ", "token": "x-y"}),
            follow=True,
        )
        assert "invalid or has already been used" in response.content.decode()

    def test_confirming_is_audited(self, client: Client) -> None:
        register(client)
        client.get(verification_link(mail.outbox[0].body))

        assert AuditLog.objects.filter(action=AuditAction.EMAIL_VERIFIED).count() == 1

    def test_the_link_can_be_sent_again(self, client: Client) -> None:
        register(client)
        mail.outbox.clear()

        client.post(reverse("accounts:resend-verification"))

        assert len(mail.outbox) == 1

    def test_resending_is_post_only(self, client: Client) -> None:
        """A GET that sends email can be fired by a prefetch or a scanner."""
        register(client)
        assert client.get(reverse("accounts:resend-verification")).status_code == 405


class TestVerificationGatesSending:
    """
    The one thing an unconfirmed address actually blocks.

    Not sign-in: locking somebody out of an empty dashboard helps nobody. But
    sending to real people from an address that may not exist means the
    failure notices and the replies go nowhere.
    """

    @pytest.fixture
    def sendable(self, organization, make_campaign, approved_template, make_contact):
        """A campaign with a real audience, ready but for the account check."""
        from campaigns.models import CampaignMessageType
        from campaigns.services import set_audience, set_message
        from contacts.models import ContactGroup, GroupMembership

        group = ContactGroup.objects.create(name="Everyone", organization=organization)
        GroupMembership.objects.create(group=group, contact=make_contact("A", opted_in=True))

        campaign = make_campaign("Launchable")
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
        return campaign

    def test_an_unverified_owner_cannot_launch(self, make_user, sendable) -> None:
        from campaigns.services import launch_campaign
        from core.exceptions import ValidationFailed

        unverified = make_user("new@example.com", email_verified=False)

        with pytest.raises(ValidationFailed, match="Confirm your email"):
            launch_campaign(sendable, user=unverified)

    def test_a_verified_owner_can(self, operator, sendable, recording_dispatcher) -> None:
        from campaigns.services import launch_campaign

        assert operator.email_verified is True
        launch_campaign(sendable, user=operator)  # does not raise

    def test_the_banner_appears_until_confirmed(self, client: Client) -> None:
        register(client)
        body = client.get(reverse("dashboard:home")).content.decode()
        assert "Confirm your email address" in body

        client.get(verification_link(mail.outbox[0].body))

        body = client.get(reverse("dashboard:home")).content.decode()
        assert "Confirm your email address" not in body


class TestPasswordReset:
    def test_a_link_is_sent_for_a_known_address(self, client: Client, operator) -> None:
        client.post(reverse("accounts:password-reset"), {"email": operator.email})
        assert len(mail.outbox) == 1

    def test_an_unknown_address_looks_identical(self, client: Client) -> None:
        """
        Same page, no email. Saying "no such account" would turn the form into
        a way to discover who has one.
        """
        response = client.post(
            reverse("accounts:password-reset"), {"email": "nobody@example.com"}, follow=True
        )

        assert "a reset link is on its way" in response.content.decode()
        assert mail.outbox == []

    def test_the_link_sets_a_new_password(self, client: Client, operator) -> None:
        client.post(reverse("accounts:password-reset"), {"email": operator.email})

        # Django's confirm view redirects to a session-backed URL before it
        # will accept the new password.
        response = client.get(reset_link(mail.outbox[0].body), follow=True)
        client.post(
            response.redirect_chain[-1][0],
            {
                "new_password1": "a-brand-new-passphrase",
                "new_password2": "a-brand-new-passphrase",
            },
        )

        operator.refresh_from_db()
        assert operator.check_password("a-brand-new-passphrase")

    def test_every_reset_page_renders(self, client: Client) -> None:
        for name in ["password-reset", "password-reset-sent", "password-reset-complete"]:
            assert client.get(reverse(f"accounts:{name}")).status_code == 200, name


class TestAbuseLimits:
    """
    The unauthenticated doors that write rows or send mail.

    Registration is the one that matters most: it is the only endpoint a
    stranger can use to both create records and point our mail server at an
    address of their choosing.
    """

    def test_a_network_cannot_create_unlimited_accounts(self, client: Client, settings) -> None:
        settings.SIGNUP_LIMIT = 2

        for n in range(2):
            register(client, email=f"a{n}@example.com")
            client.logout()

        response = register(client, email="a2@example.com")

        assert response.status_code == 200
        assert "Too many accounts" in response.content.decode()
        assert not User.objects.filter(email="a2@example.com").exists()

    def test_the_form_keeps_what_they_typed(self, client: Client, settings) -> None:
        """The likeliest person to hit this is an office signing colleagues up."""
        settings.SIGNUP_LIMIT = 1
        register(client, email="a0@example.com")
        client.logout()

        body = register(client, email="blocked@example.com").content.decode()

        assert "blocked@example.com" in body

    def test_a_rejected_form_does_not_count(self, client: Client, settings) -> None:
        """Nothing was created and nothing was sent, so nothing was spent."""
        settings.SIGNUP_LIMIT = 2

        for _ in range(5):
            register(client, password2="does-not-match")

        assert register(client).status_code == 302  # still allowed through

    def test_the_limit_can_be_disabled(self, client: Client, settings) -> None:
        settings.SIGNUP_LIMIT = 0

        for n in range(3):
            register(client, email=f"b{n}@example.com")
            client.logout()

        assert User.objects.filter(email__startswith="b").count() == 3

    def test_password_reset_mail_is_capped(self, client: Client, operator, settings) -> None:
        settings.OUTBOUND_EMAIL_LIMIT = 2

        for _ in range(2):
            client.post(reverse("accounts:password-reset"), {"email": operator.email})
        response = client.post(reverse("accounts:password-reset"), {"email": operator.email})

        assert len(mail.outbox) == 2
        assert "Too many emails" in response.content.decode()

    def test_the_reset_cap_counts_unknown_addresses_too(
        self, client: Client, operator, settings
    ) -> None:
        """
        Otherwise the block itself says which addresses are registered — undoing
        the identical response the form goes to the trouble of giving.
        """
        settings.OUTBOUND_EMAIL_LIMIT = 2

        for _ in range(2):
            client.post(reverse("accounts:password-reset"), {"email": "nobody@example.com"})
        response = client.post(reverse("accounts:password-reset"), {"email": operator.email})

        assert mail.outbox == []
        assert "Too many emails" in response.content.decode()

    def test_resending_confirmation_is_capped(self, client: Client, settings) -> None:
        settings.OUTBOUND_EMAIL_LIMIT = 2
        register(client)
        mail.outbox.clear()

        for _ in range(3):
            client.post(reverse("accounts:resend-verification"))

        assert len(mail.outbox) == 2
