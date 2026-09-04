"""
Telling customers things, without becoming a mailing list.

Almost every test here is about *not* sending. That is the shape of the problem:
the easy version of this feature emails everybody about everything, and the work
is in the four refusals — no preference, no confirmed address, no duplicate, and
no failure that takes the real work down with it.
"""

from __future__ import annotations

import pytest
from django.core import mail

from core.models import (
    NOTIFICATION_DEFAULTS,
    NotificationKind,
    NotificationLog,
    NotificationPreference,
)
from core.notifications import notify, notify_organization
from organizations.models import OrganizationMember, OrganizationRole

pytestmark = pytest.mark.django_db

TEMPLATE = "notifications/campaign_finished.txt"


def send(user, kind=NotificationKind.CAMPAIGN_FAILED, **kwargs):
    return notify(
        user=user,
        kind=kind,
        subject="Something happened",
        template=TEMPLATE,
        context={"campaign": None, "total": 1, "failed": 1, "delivered": 0, "heavy": True},
        **kwargs,
    )


class TestItOnlySendsWhatWasAskedFor:
    def test_a_kind_that_defaults_on_is_sent(self, operator) -> None:
        assert send(operator) is True
        assert len(mail.outbox) == 1

    def test_a_kind_that_defaults_off_is_not(self, operator) -> None:
        """
        Being told a payment failed is a service. Being told every campaign
        finished is a mailing list, so it is off until somebody asks.
        """
        assert NOTIFICATION_DEFAULTS[NotificationKind.CAMPAIGN_FINISHED] is False

        assert send(operator, kind=NotificationKind.CAMPAIGN_FINISHED) is False
        assert mail.outbox == []

    def test_turning_one_off_is_respected(self, operator) -> None:
        NotificationPreference.objects.create(
            user=operator, kind=NotificationKind.CAMPAIGN_FAILED, enabled=False
        )

        assert send(operator) is False
        assert mail.outbox == []

    def test_turning_one_on_is_respected(self, operator) -> None:
        NotificationPreference.objects.create(
            user=operator, kind=NotificationKind.CAMPAIGN_FINISHED, enabled=True
        )

        assert send(operator, kind=NotificationKind.CAMPAIGN_FINISHED) is True

    def test_no_preference_means_the_default_not_yes(self, operator) -> None:
        """Nobody is opted in to anything by never having been asked."""
        assert not NotificationPreference.objects.filter(user=operator).exists()

        assert NotificationPreference.objects.wants(
            operator, NotificationKind.CAMPAIGN_FINISHED
        ) is False


class TestItWillNotMailAnUnconfirmedAddress:
    def test_an_unverified_user_gets_nothing(self, make_user) -> None:
        """
        Same rule Stage 2 applied to sending campaigns. Mail to an address
        nobody has confirmed is at best noise and at worst somebody else's inbox.
        """
        unverified = make_user("new@example.com", email_verified=False)

        assert send(unverified) is False
        assert mail.outbox == []

    def test_a_deactivated_user_gets_nothing(self, make_user) -> None:
        gone = make_user("gone@example.com", is_active=False)

        assert send(gone) is False

    def test_a_user_with_no_address_gets_nothing(self, operator) -> None:
        operator.email = ""

        assert send(operator) is False


class TestItWillNotSendTwice:
    def test_the_same_key_sends_once(self, operator) -> None:
        """
        A campaign finishing while a worker retries must not produce two emails.
        The recipient cannot tell a retry from a real second event.
        """
        send(operator, idempotency_key="campaign-finished:abc")
        second = send(operator, idempotency_key="campaign-finished:abc")

        assert second is False
        assert len(mail.outbox) == 1

    def test_different_keys_both_send(self, operator) -> None:
        send(operator, idempotency_key="campaign-finished:one")
        send(operator, idempotency_key="campaign-finished:two")

        assert len(mail.outbox) == 2

    def test_the_row_is_written_before_the_mail(self, operator) -> None:
        """
        The other way round would send twice whenever the recording failed,
        which is the wrong direction for something a person receives.
        """
        send(operator, idempotency_key="ordering")

        entry = NotificationLog.objects.get(idempotency_key="ordering")
        assert entry.sent_at is not None

    def test_the_constraint_is_the_guard_not_a_lookup(self, operator) -> None:
        """Two workers would both pass an `.exists()` check and both send."""
        from django.db import IntegrityError, transaction

        send(operator, idempotency_key="raced")

        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationLog.objects.create(
                user=operator, kind=NotificationKind.CAMPAIGN_FAILED, idempotency_key="raced"
            )


class TestAFailureNeverBreaksTheCaller:
    def test_a_missing_template_does_not_raise(self, operator) -> None:
        """
        Every caller is finishing real work when it reaches here. None of them
        should fail because a template was renamed.
        """
        result = notify(
            user=operator,
            kind=NotificationKind.CAMPAIGN_FAILED,
            subject="x",
            template="notifications/does_not_exist.txt",
        )

        assert result is False

    def test_a_broken_mail_server_does_not_raise(self, operator, monkeypatch) -> None:
        def explode(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr("core.notifications.send_mail", explode)

        assert send(operator) is False

    def test_a_none_user_does_not_raise(self) -> None:
        assert send(None) is False


class TestWhoHearsAboutIt:
    @pytest.fixture
    def team(self, organization, make_user):
        member = make_user("member@example.com")
        OrganizationMember.objects.create(
            organization=organization, user=member, role=OrganizationRole.MEMBER
        )
        return member

    def test_billing_news_goes_to_administrators_only(self, organization, team) -> None:
        """A member who cannot change the card does not need to be told it failed."""
        sent = notify_organization(
            organization=organization,
            kind=NotificationKind.PAYMENT_FAILED,
            subject="Payment failed",
            template="notifications/payment_failed.txt",
            context={"invoice": None, "organization": organization},
        )

        assert sent == 1
        assert team.email not in [address for message in mail.outbox for address in message.to]

    def test_campaign_news_goes_to_everybody_who_wants_it(
        self, organization, team, operator
    ) -> None:
        for user in (operator, team):
            NotificationPreference.objects.create(
                user=user, kind=NotificationKind.CAMPAIGN_FINISHED, enabled=True
            )

        sent = notify_organization(
            organization=organization,
            kind=NotificationKind.CAMPAIGN_FINISHED,
            subject="Finished",
            template=TEMPLATE,
            context={"campaign": None, "total": 1, "failed": 0, "delivered": 1, "heavy": False},
        )

        assert sent == 2

    def test_each_recipient_gets_their_own_idempotency_key(self, organization, team) -> None:
        """One shared key would mean only the first person in the list is told."""
        for user in (organization.owner, team):
            NotificationPreference.objects.create(
                user=user, kind=NotificationKind.CAMPAIGN_FINISHED, enabled=True
            )

        notify_organization(
            organization=organization,
            kind=NotificationKind.CAMPAIGN_FINISHED,
            subject="Finished",
            template=TEMPLATE,
            context={"campaign": None, "total": 1, "failed": 0, "delivered": 1, "heavy": False},
            idempotency_key="campaign-finished:xyz",
        )

        assert NotificationLog.objects.count() == 2


class TestThePreferencesPage:
    def test_it_lists_every_kind(self, auth_client) -> None:
        """
        Exhaustively, not only the ones somebody has an opinion about. A page
        that hides the settings you have never touched is one you cannot use to
        find anything.
        """
        from django.urls import reverse

        body = auth_client.get(reverse("accounts:profile")).content.decode()

        for value, _label in NotificationKind.choices:
            assert value in body, value

    def test_saving_records_both_answers(self, auth_client, operator) -> None:
        """
        An unticked checkbox submits nothing at all, so reading only what
        arrived would make "off" unrepresentable and silently revert it.
        """
        from django.urls import reverse

        auth_client.post(
            reverse("accounts:profile"),
            {"save_notifications": "1", "notifications": [NotificationKind.CAMPAIGN_FINISHED]},
        )

        assert NotificationPreference.objects.wants(
            operator, NotificationKind.CAMPAIGN_FINISHED
        ) is True
        assert NotificationPreference.objects.wants(
            operator, NotificationKind.PAYMENT_FAILED
        ) is False, "an unticked box must turn its kind off, not leave it at the default"

    def test_turning_everything_off_is_possible(self, auth_client, operator) -> None:
        from django.urls import reverse

        auth_client.post(reverse("accounts:profile"), {"save_notifications": "1"})

        assert all(
            not NotificationPreference.objects.wants(operator, value)
            for value, _ in NotificationKind.choices
        )

    def test_it_survives_a_round_trip(self, auth_client, operator) -> None:
        from django.urls import reverse

        auth_client.post(
            reverse("accounts:profile"),
            {"save_notifications": "1", "notifications": [NotificationKind.SENDER_PROBLEM]},
        )

        response = auth_client.get(reverse("accounts:profile"))
        state = {kind["value"]: kind["enabled"] for kind in response.context["notification_kinds"]}

        assert state[NotificationKind.SENDER_PROBLEM] is True
        assert state[NotificationKind.CAMPAIGN_FAILED] is False
