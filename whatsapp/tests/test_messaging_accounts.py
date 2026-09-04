"""
Per-organization messaging credentials.

Three things are being protected here, in descending order of how bad it would
be to get them wrong:

1. **A customer's message goes out on their own number, with their own token.**
   Sending one tenant's campaign with another's credentials would put their
   messages on the wrong sender's reputation and the wrong messaging limit.
2. **An inbound STOP withdraws the right customer's consent.** Two customers can
   hold the same person as a contact. Matching on the sender's number alone
   opted out whichever row came back first, which is a consent bug.
3. **A token is never readable from the database alone.** It is encrypted with a
   key that lives in the environment, and it is never logged or rendered.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from core.encryption import DecryptionFailed, decrypt, encrypt, generate_key
from whatsapp.accounts import MessagingAccount, MessagingAccountStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def make_account(db):
    def _make(organization, *, token="EAAtest-token-1234567890", **fields):
        defaults = {
            "provider": "meta",
            "phone_number_id": f"pn-{MessagingAccount.objects.count() + 1}",
            "waba_id": "waba-1",
            "status": MessagingAccountStatus.ACTIVE,
            "is_default": True,
        }
        account = MessagingAccount(organization=organization, **{**defaults, **fields})
        account.access_token = token
        account.save()
        return account

    return _make


class TestEncryption:
    def test_a_token_round_trips(self) -> None:
        assert decrypt(encrypt("EAAsecret")) == "EAAsecret"

    def test_the_same_value_encrypts_differently_each_time(self) -> None:
        """Otherwise equal ciphertexts reveal which customers share a token."""
        assert encrypt("same") != encrypt("same")

    def test_the_plaintext_is_not_in_the_stored_value(self, organization, make_account) -> None:
        account = make_account(organization, token="EAAsupersecrettoken")

        assert "EAAsupersecrettoken" not in account.access_token_encrypted

    def test_an_empty_value_stays_empty(self) -> None:
        """Encrypting "" would store ciphertext where "not set" is the truth."""
        assert encrypt("") == ""
        assert decrypt("") == ""

    def test_a_tampered_value_is_refused_not_silently_wrong(self) -> None:
        """
        Fernet is authenticated. A token that decrypted to rubbish would be sent
        to a provider as though it were real.
        """
        corrupted = encrypt("EAAtoken")[:-6] + "aaaaaa"

        with pytest.raises(DecryptionFailed):
            decrypt(corrupted)

    def test_a_changed_key_fails_loudly(self, organization, make_account, settings) -> None:
        account = make_account(organization)
        settings.FIELD_ENCRYPTION_KEY = generate_key()

        reloaded = MessagingAccount.objects.get(pk=account.pk)
        with pytest.raises(DecryptionFailed):
            _ = reloaded.access_token

    def test_the_error_never_contains_the_value(self) -> None:
        try:
            decrypt(encrypt("EAAsupersecret")[:-6] + "aaaaaa")
        except DecryptionFailed as exc:
            assert "EAAsupersecret" not in str(exc)


class TestTheTokenIsNotAField:
    def test_it_is_absent_from_the_model_fields(self) -> None:
        """
        Which is what keeps it out of ModelForms, ModelSerializers, values()
        and the admin's default field list without anyone having to remember.
        """
        names = {field.name for field in MessagingAccount._meta.fields}

        assert "access_token" not in names
        assert "access_token_encrypted" in names

    def test_the_hint_identifies_without_revealing(self, organization, make_account) -> None:
        account = make_account(organization, token="EAAabcdefghijklmnop")

        assert account.token_hint == "…mnop"
        assert "abcdefghij" not in account.token_hint

    def test_a_short_token_gets_no_hint_at_all(self, organization, make_account) -> None:
        """Four characters of a short token narrow it down too far."""
        assert make_account(organization, token="short").token_hint == "set"


class TestValidation:
    def test_a_live_provider_needs_a_token(self, organization) -> None:
        account = MessagingAccount(
            organization=organization, provider="meta", phone_number_id="pn-x"
        )

        with pytest.raises(ValidationError):
            account.full_clean()

    def test_the_mock_does_not(self, organization) -> None:
        """It sends nothing anywhere, so there is nothing to authenticate."""
        account = MessagingAccount(
            organization=organization, provider="mock", phone_number_id="pn-mock"
        )
        account.full_clean()  # does not raise

    def test_an_unknown_provider_is_refused(self, organization) -> None:
        account = MessagingAccount(
            organization=organization, provider="carrier-pigeon", phone_number_id="pn-y"
        )

        with pytest.raises(ValidationError):
            account.full_clean()

    def test_one_number_belongs_to_one_organization(
        self, organization, other_organization, make_account
    ) -> None:
        """Inbound webhooks route by this, so two claimants would be ambiguous."""
        from django.db import IntegrityError, transaction

        make_account(organization, phone_number_id="shared")

        with pytest.raises(IntegrityError), transaction.atomic():
            make_account(other_organization, phone_number_id="shared")

    def test_only_one_default_per_organization(self, organization, make_account) -> None:
        from django.db import IntegrityError, transaction

        make_account(organization, phone_number_id="pn-a", is_default=True)

        with pytest.raises(IntegrityError), transaction.atomic():
            make_account(organization, phone_number_id="pn-b", is_default=True)

    def test_a_second_non_default_number_is_fine(self, organization, make_account) -> None:
        """A WABA holds several numbers; that is ordinary, not an edge case."""
        make_account(organization, phone_number_id="pn-a", is_default=True)
        make_account(organization, phone_number_id="pn-b", is_default=False)

        assert MessagingAccount.objects.for_organization(organization).count() == 2


class TestResolvingTheSender:
    def test_an_organization_sends_with_its_own_credentials(
        self, organization, make_account, settings
    ) -> None:
        settings.WHATSAPP_PROVIDER = "meta"
        make_account(organization, token="EAAmine", phone_number_id="pn-mine")

        from whatsapp.services.factory import provider_for

        provider = provider_for(organization)

        assert provider.phone_number_id == "pn-mine"
        assert provider._access_token == "EAAmine"

    def test_two_tenants_get_two_senders(
        self, organization, other_organization, make_account, settings
    ) -> None:
        """The whole point of the stage, and the worst thing to get wrong."""
        settings.WHATSAPP_PROVIDER = "meta"
        make_account(organization, token="EAAone", phone_number_id="pn-one")
        make_account(other_organization, token="EAAtwo", phone_number_id="pn-two")

        from whatsapp.services.factory import provider_for

        assert provider_for(organization)._access_token == "EAAone"
        assert provider_for(other_organization)._access_token == "EAAtwo"

    def test_a_disabled_account_is_not_used(
        self, organization, make_account, settings
    ) -> None:
        settings.WHATSAPP_PROVIDER = "meta"
        settings.META_PHONE_NUMBER_ID = "env-number"
        make_account(
            organization, phone_number_id="pn-off", status=MessagingAccountStatus.DISABLED
        )

        from whatsapp.services.factory import provider_for

        assert provider_for(organization).phone_number_id == "env-number"

    def test_without_an_account_it_falls_back_to_the_environment(
        self, organization, settings
    ) -> None:
        """What keeps every installation that predates Stage 5 working."""
        settings.WHATSAPP_PROVIDER = "meta"
        settings.META_PHONE_NUMBER_ID = "env-number"

        from whatsapp.services.factory import provider_for

        assert provider_for(organization).phone_number_id == "env-number"

    def test_the_fallback_can_be_turned_off(self, organization, settings) -> None:
        """
        A platform serving strangers wants this on: otherwise a customer with no
        sender of their own sends on the deployment's number and reputation.
        """
        from core.exceptions import ProviderNotConfigured
        from whatsapp.services.factory import provider_for

        settings.WHATSAPP_REQUIRE_MESSAGING_ACCOUNT = True

        with pytest.raises(ProviderNotConfigured, match="no WhatsApp sender connected"):
            provider_for(organization)

    def test_the_provider_is_not_cached_between_tenants(
        self, organization, other_organization, make_account, settings
    ) -> None:
        """
        A cached provider holds one tenant's token and would hand it to the
        next caller. This is why get_provider builds a new one every time.
        """
        settings.WHATSAPP_PROVIDER = "meta"
        make_account(organization, token="EAAone", phone_number_id="pn-one")
        make_account(other_organization, token="EAAtwo", phone_number_id="pn-two")

        from whatsapp.services.factory import provider_for

        first = provider_for(organization)
        second = provider_for(other_organization)

        assert first is not second
        assert first._access_token != second._access_token

    def test_a_misconfigured_account_is_reported_against_the_account(
        self, organization, make_account, settings
    ) -> None:
        """
        Not against the environment. An organization whose token is empty must
        not pass a check merely because the deployment happens to have one.
        """
        from core.exceptions import ProviderNotConfigured
        from whatsapp.services.factory import provider_for

        settings.WHATSAPP_PROVIDER = "meta"
        settings.META_ACCESS_TOKEN = "env-token-that-must-not-be-used"
        account = make_account(organization, phone_number_id="pn-empty")
        MessagingAccount.objects.filter(pk=account.pk).update(access_token_encrypted="")

        with pytest.raises(ProviderNotConfigured, match="messaging account"):
            provider_for(organization).check_configuration()


class TestInboundStopReachesTheRightTenant:
    """
    The consent bug multi-tenancy would have introduced.

    Two customers can hold the same person as a contact. Before Stage 5 the
    lookup was unscoped, so an inbound STOP opted out whichever row the database
    returned first.
    """

    @pytest.fixture
    def both_have_the_contact(
        self, organization, other_organization, make_contact, make_account
    ):
        mine = make_contact("Mine", "+9779800000001", opted_in=True)
        theirs = make_contact(
            "Theirs", "+9779800000001", opted_in=True, organization=other_organization
        )
        make_account(organization, phone_number_id="pn-mine")
        make_account(other_organization, phone_number_id="pn-theirs")
        return mine, theirs

    def _stop(self, business_phone_number_id, sender="9779800000001"):
        from whatsapp.services.base import InboundMessage
        from whatsapp.services.inbound import _handle_inbound

        return _handle_inbound(
            InboundMessage(
                from_phone_number=sender,
                text="STOP",
                business_phone_number_id=business_phone_number_id,
            )
        )

    def test_it_opts_out_only_the_organization_that_received_it(
        self, both_have_the_contact
    ) -> None:
        mine, theirs = both_have_the_contact

        assert self._stop("pn-theirs") is True

        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert theirs.opted_in is False
        assert mine.opted_in is True, "the wrong tenant's consent was withdrawn"

    def test_an_unknown_receiving_number_is_logged(
        self, both_have_the_contact, caplog
    ) -> None:
        # Named, because the project's loggers do not propagate to root.
        with caplog.at_level("WARNING", logger="whatsapp.services.inbound"):
            self._stop("pn-nobody-claims")

        assert "no messaging account claims" in caplog.text

    def test_it_still_works_for_a_single_tenant_install(
        self, organization, make_contact
    ) -> None:
        """
        No messaging accounts at all, no receiving number in the payload: the
        pre-Stage-5 shape, which must keep behaving exactly as it did.
        """
        contact = make_contact("Only", "+9779800000009", opted_in=True)

        assert self._stop("", sender="9779800000009") is True

        contact.refresh_from_db()
        assert contact.opted_in is False


class TestTheParserCapturesTheReceivingNumber:
    def test_it_reads_the_metadata(self) -> None:
        from whatsapp.services.meta_cloud_api import MetaWhatsAppProvider

        _, messages = MetaWhatsAppProvider().parse_webhook(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": {"phone_number_id": "pn-received-on"},
                                    "messages": [
                                        {
                                            "from": "9779800000001",
                                            "id": "wamid.1",
                                            "text": {"body": "STOP"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ]
            }
        )

        assert messages[0].business_phone_number_id == "pn-received-on"

    def test_a_payload_without_metadata_does_not_break(self) -> None:
        from whatsapp.services.meta_cloud_api import MetaWhatsAppProvider

        _, messages = MetaWhatsAppProvider().parse_webhook(
            {
                "entry": [
                    {"changes": [{"value": {"messages": [{"from": "977", "text": {"body": "hi"}}]}}]}
                ]
            }
        )

        assert messages[0].business_phone_number_id == ""
