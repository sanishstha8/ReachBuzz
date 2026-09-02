"""
The live verification command.

Its whole purpose is to talk to Meta, so these tests stub Meta and check the
*decisions* it makes about what it hears: which conditions are fatal, which are
warnings, and — the part that matters most — that it cannot be talked into
messaging someone who has not consented.

That last one is why the command is tested at all. It is the only place in the
project that sends a message from a command line with a phone number as an
argument, which is exactly the shape of thing that grows a ``--force`` flag
later. The consent check has no override, and these tests are what keeps it
that way.
"""

from __future__ import annotations

from io import StringIO

import pytest
import responses
from django.core.management import call_command
from django.core.management.base import CommandError

from contacts.models import ContactStatus
from whatsapp.models import MessageTemplate

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "https://graph.facebook.com/vTEST/456/message_templates"


@pytest.fixture
def meta(settings):
    settings.WHATSAPP_PROVIDER = "meta"
    settings.META_API_VERSION = "vTEST"
    settings.META_ACCESS_TOKEN = "EAAtoken"
    settings.META_PHONE_NUMBER_ID = "123"
    settings.META_WABA_ID = "456"
    settings.META_APP_SECRET = "secret"
    settings.META_WEBHOOK_VERIFY_TOKEN = "verify"
    return settings


def approved_payload(**overrides) -> dict:
    template = {
        "name": "order_ready",
        "language": "en_US",
        "status": "APPROVED",
        "category": "UTILITY",
        "id": "1",
        "components": [{"type": "BODY", "text": "Hello {{name}}"}],
    }
    template.update(overrides)
    return template


@pytest.fixture(autouse=True)
def sending_available(celery_dispatcher):
    """
    Register the real dispatcher for this module.

    The project clears it before every test so the "sending unavailable" path
    stays honest. This command reports an unregistered sender as a failure —
    correctly, since nothing could be sent — so the checks that expect a clean
    run need one present.
    """
    return celery_dispatcher


def run(**options) -> str:
    out = StringIO()
    call_command("verify_live", stdout=out, stderr=out, **options)
    return out.getvalue()


class TestItRefusesToVerifyNothing:
    def test_the_mock_provider_is_refused(self) -> None:
        """Against the mock it would only be testing itself."""
        with pytest.raises(CommandError, match="mock"):
            run()


class TestReadOnlyChecks:
    def test_an_unregistered_sender_is_a_failure(self, meta, http) -> None:
        """
        A pipeline nothing can send through is exactly what this command is
        for finding, so it must be fatal rather than a note.
        """
        from campaigns import dispatch

        http.add(responses.GET, TEMPLATES_URL, json={"data": [approved_payload()]}, status=200)
        dispatch.clear_dispatcher()

        with pytest.raises(SystemExit):
            run()

    def test_a_healthy_configuration_reports_no_failures(self, meta, http) -> None:
        http.add(responses.GET, TEMPLATES_URL, json={"data": [approved_payload()]}, status=200)

        output = run()

        assert "FAIL" not in output
        assert "1 approved" in output

    def test_nothing_is_sent_without_an_explicit_recipient(self, meta, http) -> None:
        http.add(responses.GET, TEMPLATES_URL, json={"data": [approved_payload()]}, status=200)

        output = run()

        assert "Nothing has left this machine" in output

    def test_missing_send_credentials_are_fatal(self, meta, settings) -> None:
        settings.META_ACCESS_TOKEN = ""

        with pytest.raises(SystemExit):
            run()

    def test_a_credential_is_never_printed(self, meta, http, settings) -> None:
        settings.META_ACCESS_TOKEN = "EAAsupersecrettoken1234567890"
        http.add(responses.GET, TEMPLATES_URL, json={"data": [approved_payload()]}, status=200)

        output = run()

        assert "EAAsupersecrettoken1234567890" not in output
        assert "secret" not in output.lower().replace("app_secret", "")

    def test_missing_optional_credentials_warn_rather_than_fail(
        self, meta, http, settings
    ) -> None:
        """
        You can send without a WABA id or an app secret; you just cannot sync
        templates or verify webhooks. That is a warning, not a broken system.
        """
        settings.META_WABA_ID = ""
        settings.META_APP_SECRET = ""

        output = run()

        assert "WARN" in output
        assert "META_WABA_ID" in output

    def test_a_rejected_token_is_reported_as_a_failure(self, meta, http) -> None:
        http.add(
            responses.GET,
            TEMPLATES_URL,
            json={"error": {"code": 190, "message": "Access token expired"}},
            status=401,
        )

        with pytest.raises(SystemExit):
            run()

    def test_no_approved_templates_is_a_warning(self, meta, http) -> None:
        http.add(
            responses.GET,
            TEMPLATES_URL,
            json={"data": [approved_payload(status="PENDING")]},
            status=200,
        )

        output = run()

        assert "WARN" in output
        assert "none approved" in output

    def test_sync_writes_the_templates_locally(self, meta, http) -> None:
        http.add(responses.GET, TEMPLATES_URL, json={"data": [approved_payload()]}, status=200)

        run(sync=True)

        assert MessageTemplate.objects.filter(name="order_ready", status="approved").exists()


class TestConsentCannotBeBypassed:
    """
    The command takes a phone number on the command line, which is the shape of
    thing that grows a --force flag later. It resolves through
    Contact.objects.eligible() and there is no way around it.
    """

    @pytest.fixture(autouse=True)
    def templates(self, meta, http, approved_template):
        http.add(responses.GET, TEMPLATES_URL, json={"data": [approved_payload()]}, status=200)

    def test_an_unknown_number_is_refused(self) -> None:
        with pytest.raises(CommandError, match="not an opted-in, active contact"):
            run(to="+9779800009999")

    def test_a_contact_who_has_not_consented_is_refused(self, make_contact) -> None:
        make_contact("No consent", "+9779800001111", opted_in=False)

        with pytest.raises(CommandError, match="not an opted-in, active contact"):
            run(to="+9779800001111")

    def test_a_consenting_but_blocked_contact_is_refused(self, make_contact) -> None:
        """Consent alone is not eligibility; the contact must also be active."""
        make_contact("Blocked", "+9779800002222", opted_in=True, status=ContactStatus.BLOCKED)

        with pytest.raises(CommandError, match="not an opted-in, active contact"):
            run(to="+9779800002222")

    def test_the_refusal_explains_how_to_record_consent_properly(self, make_contact) -> None:
        """Pointing at create_contact, not at a way around the check."""
        with pytest.raises(CommandError) as exc_info:
            run(to="+9779800009999")

        message = str(exc_info.value)
        assert "create_contact" in message
        assert "opted_in=True" in message

    def test_an_unparseable_number_is_refused_clearly(self) -> None:
        with pytest.raises(CommandError, match="not a usable phone number"):
            run(to="not-a-number")

    def test_no_message_is_created_by_a_refusal(self, make_contact) -> None:
        from messaging.models import Message

        make_contact("No consent", "+9779800001111", opted_in=False)

        with pytest.raises(CommandError):
            run(to="+9779800001111")

        assert Message.objects.count() == 0


class TestTemplateSelection:
    @pytest.fixture(autouse=True)
    def templates(self, meta, http):
        http.add(responses.GET, TEMPLATES_URL, json={"data": [approved_payload()]}, status=200)

    def test_a_named_template_that_does_not_exist_is_refused(self, make_contact) -> None:
        make_contact("Consenting", "+9779800003333", opted_in=True)

        with pytest.raises(CommandError, match="No approved, synced template"):
            run(to="+9779800003333", template="nonexistent")

    def test_sending_without_any_approved_template_is_refused(self, make_contact) -> None:
        make_contact("Consenting", "+9779800003333", opted_in=True)

        with pytest.raises(CommandError, match="No approved template is mirrored"):
            run(to="+9779800003333")
