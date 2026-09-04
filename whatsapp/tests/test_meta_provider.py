"""
The Meta Cloud API provider.

Every request is stubbed: the suite must pass with no credentials and no
network, and an autouse fixture makes an unregistered call an error rather than
a phone call to Meta.

The tests that matter most are about *decisions*, not about payload shapes:
which failures are worth retrying, and which are not. A wrong answer there does
not raise — it quietly burns quota against a dead number, or gives up on a send
that would have succeeded on the second attempt.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import responses

from core.exceptions import ProviderNotConfigured
from whatsapp.services.meta_cloud_api import MetaWhatsAppProvider

MESSAGES_URL = "https://graph.facebook.com/vTEST/123/messages"
TEMPLATES_URL = "https://graph.facebook.com/vTEST/456/message_templates"

TOKEN = "EAAsupersecrettoken1234567890"
APP_SECRET = "topsecretappsecret"


@pytest.fixture(autouse=True)
def meta_configured(settings):
    """Credentials that exist only for this module, and never leave it."""
    settings.WHATSAPP_PROVIDER = "meta"
    settings.META_API_VERSION = "vTEST"
    settings.META_ACCESS_TOKEN = TOKEN
    settings.META_PHONE_NUMBER_ID = "123"
    settings.META_WABA_ID = "456"
    settings.META_APP_SECRET = APP_SECRET
    settings.WHATSAPP_REQUEST_TIMEOUT = 5
    return settings


ACCEPTED = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "9779800000001", "wa_id": "9779800000001"}],
    "messages": [{"id": "wamid.TEST123", "message_status": "accepted"}],
}


def meta_error(code, *, message="Something went wrong", details="", subcode=None) -> dict:
    """An error body in the shape Meta documents."""
    error = {
        "message": message,
        "type": "OAuthException",
        "code": code,
        "error_data": {"messaging_product": "whatsapp", "details": details},
        "fbtrace_id": "Atrace123",
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    return {"error": error}


def sent_body(http) -> dict:
    return json.loads(http.calls[0].request.body)


class TestSendPayloads:
    def test_a_text_message_matches_the_documented_shape(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, json=ACCEPTED, status=200)

        MetaWhatsAppProvider().send_text(to="+9779800000001", body="Hello there")

        assert sent_body(http) == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "9779800000001",
            "type": "text",
            "text": {"body": "Hello there"},
        }

    def test_a_template_message_matches_the_documented_shape(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, json=ACCEPTED, status=200)

        MetaWhatsAppProvider().send_template(
            to="+9779800000001",
            template_name="order_ready",
            language="en_US",
            body_variables=["Aarav", "A-100"],
        )

        assert sent_body(http)["template"] == {
            "name": "order_ready",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Aarav"},
                        {"type": "text", "text": "A-100"},
                    ],
                }
            ],
        }

    def test_a_template_with_no_variables_sends_no_components(self, http) -> None:
        """Meta rejects an empty components array."""
        http.add(responses.POST, MESSAGES_URL, json=ACCEPTED, status=200)

        MetaWhatsAppProvider().send_template(
            to="+9779800000001", template_name="hello", language="en"
        )

        assert "components" not in sent_body(http)["template"]

    def test_header_variables_become_their_own_component(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, json=ACCEPTED, status=200)

        MetaWhatsAppProvider().send_template(
            to="+9779800000001",
            template_name="promo",
            language="en",
            body_variables=["body"],
            header_variables=["header"],
        )

        components = sent_body(http)["template"]["components"]
        assert [c["type"] for c in components] == ["header", "body"]

    def test_the_recipient_loses_its_leading_plus(self, http) -> None:
        """We store E.164; Meta's examples and its webhooks both omit the plus."""
        http.add(responses.POST, MESSAGES_URL, json=ACCEPTED, status=200)

        MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert sent_body(http)["to"] == "9779800000001"

    def test_the_token_is_sent_as_a_bearer_header_and_nowhere_else(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, json=ACCEPTED, status=200)

        MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        request = http.calls[0].request
        body = request.body or b""
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in (body.decode() if isinstance(body, bytes) else body)


class TestSendOutcomes:
    def test_a_successful_send_returns_the_wamid(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, json=ACCEPTED, status=200)

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert result.success is True
        assert result.provider_message_id == "wamid.TEST123"

    def test_a_200_with_no_message_id_is_a_retryable_failure(self, http) -> None:
        """
        Without a wamid no webhook can ever be matched back to this message, so
        it is not a send we can track — treating it as success would strand it.
        """
        http.add(responses.POST, MESSAGES_URL, json={"messages": []}, status=200)

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert result.success is False
        assert result.retryable is True

    @pytest.mark.parametrize("code", [4, 80007, 130429, 131000, 131016, 133004, 2494100])
    def test_transient_provider_errors_are_retried(self, http, code: int) -> None:
        http.add(responses.POST, MESSAGES_URL, json=meta_error(code), status=400)

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert result.success is False
        assert result.retryable is True, code
        assert result.error_code == str(code)

    @pytest.mark.parametrize("code", [100, 190, 368, 131026, 131047, 131051, 132000, 133010])
    def test_permanent_provider_errors_are_not_retried(self, http, code: int) -> None:
        """Retrying these burns quota against an outcome that will not change."""
        http.add(responses.POST, MESSAGES_URL, json=meta_error(code), status=400)

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert result.success is False
        assert result.retryable is False, code

    @pytest.mark.parametrize("code", [131049, 131048])
    def test_per_recipient_limits_are_never_retried(self, http, code: int) -> None:
        """
        These look transient and are not.

        Meta documents that retrying them "artificially lowers your perceived
        delivery rate, as the same per-user limit may still be in effect" — so
        a retry costs us the metric and changes nothing. Backing off is also
        the only reading consistent with never pushing against a limit.
        """
        http.add(responses.POST, MESSAGES_URL, json=meta_error(code), status=400)

        assert MetaWhatsAppProvider().send_text(to="+97798000001", body="hi").retryable is False

    def test_a_server_error_with_no_code_is_retried(self, http) -> None:
        """The one case where the HTTP status is all there is to go on."""
        http.add(responses.POST, MESSAGES_URL, body="upstream exploded", status=503)

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert result.retryable is True
        assert result.error_code == "http_503"

    def test_a_client_error_with_no_code_is_not_retried(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, body="nope", status=400)

        assert MetaWhatsAppProvider().send_text(to="+97798000001", body="hi").retryable is False

    def test_a_timeout_is_retried(self, http) -> None:
        import requests

        http.add(responses.POST, MESSAGES_URL, body=requests.Timeout("too slow"))

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert result.retryable is True
        assert result.error_code == "timeout"

    def test_a_connection_failure_is_retried_without_leaking_the_url(self, http) -> None:
        """A requests exception stringifies to the full URL, which carries ids."""
        import requests

        http.add(responses.POST, MESSAGES_URL, body=requests.ConnectionError("boom"))

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        assert result.retryable is True
        assert "graph.facebook.com" not in result.error_message

    def test_a_retry_after_header_is_honoured_when_present(self, http) -> None:
        http.add(
            responses.POST,
            MESSAGES_URL,
            json=meta_error(130429),
            status=429,
            headers={"Retry-After": "42"},
        )

        assert MetaWhatsAppProvider().send_text(to="+97798000001", body="hi").retry_after == 42

    def test_no_retry_after_falls_back_to_our_own_backoff(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, json=meta_error(130429), status=429)

        assert MetaWhatsAppProvider().send_text(to="+97798000001", body="hi").retry_after is None

    def test_the_provider_detail_is_preferred_over_the_generic_title(self, http) -> None:
        http.add(
            responses.POST,
            MESSAGES_URL,
            json=meta_error(131026, message="(#131026) Unable to deliver", details="Not on WhatsApp"),
            status=400,
        )

        assert MetaWhatsAppProvider().send_text(to="+97798000001", body="hi").error_message == (
            "Not on WhatsApp"
        )

    def test_no_credential_appears_in_any_failure(self, http) -> None:
        http.add(responses.POST, MESSAGES_URL, json=meta_error(190), status=401)

        result = MetaWhatsAppProvider().send_text(to="+9779800000001", body="hi")

        blob = f"{result.error_code}{result.error_message}{result.raw}"
        assert TOKEN not in blob
        assert APP_SECRET not in blob


class TestWebhookSignature:
    SECRET = APP_SECRET.encode()

    def sign(self, body: bytes) -> str:
        return "sha256=" + hmac.new(self.SECRET, body, hashlib.sha256).hexdigest()

    def test_a_correct_signature_verifies(self) -> None:
        body = b'{"object":"whatsapp_business_account"}'
        assert MetaWhatsAppProvider().verify_webhook_signature(body, self.sign(body)) is True

    def test_a_tampered_body_does_not_verify(self) -> None:
        signature = self.sign(b'{"amount": 1}')
        assert MetaWhatsAppProvider().verify_webhook_signature(b'{"amount": 999}', signature) is False

    def test_the_hash_is_over_the_raw_bytes_not_the_reparsed_json(self) -> None:
        """
        Re-serialising the parsed payload changes whitespace and key order, so
        a signature computed over it would never match. This pins the raw-body
        requirement so nobody "tidies" the view into parsing first.
        """
        raw = b'{"b": 1,   "a": 2}'
        reserialised = json.dumps(json.loads(raw)).encode()

        provider = MetaWhatsAppProvider()

        assert provider.verify_webhook_signature(raw, self.sign(raw)) is True
        assert provider.verify_webhook_signature(reserialised, self.sign(raw)) is False

    @pytest.mark.parametrize(
        "header", ["", "garbage", "sha1=abc123", "sha256=", "abc123", "sha256"]
    )
    def test_a_malformed_header_is_refused(self, header: str) -> None:
        assert MetaWhatsAppProvider().verify_webhook_signature(b"{}", header) is False

    def test_verification_without_a_secret_is_an_error_not_a_pass(self, settings) -> None:
        """Silently returning True here would make the endpoint world-writable."""
        settings.META_APP_SECRET = ""
        with pytest.raises(ProviderNotConfigured, match="META_APP_SECRET"):
            MetaWhatsAppProvider().verify_webhook_signature(b"{}", "sha256=abc")


class TestParseWebhook:
    def status_payload(self, **overrides) -> dict:
        status = {
            "id": "wamid.TEST123",
            "status": "delivered",
            "timestamp": "1749416383",
            "recipient_id": "9779800000001",
        }
        status.update(overrides)
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "123"},
                                "statuses": [status],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

    def test_a_delivery_status_is_extracted(self) -> None:
        statuses, messages = MetaWhatsAppProvider().parse_webhook(self.status_payload())

        assert messages == []
        assert len(statuses) == 1
        assert statuses[0].provider_message_id == "wamid.TEST123"
        assert statuses[0].status == "delivered"
        assert statuses[0].timestamp is not None

    @pytest.mark.parametrize(
        ("meta_status", "expected"),
        [("sent", "sent"), ("delivered", "delivered"), ("read", "read"), ("failed", "failed")],
    )
    def test_every_status_we_can_act_on_is_mapped(self, meta_status, expected) -> None:
        statuses, _ = MetaWhatsAppProvider().parse_webhook(
            self.status_payload(status=meta_status)
        )
        assert statuses[0].status == expected

    def test_a_status_we_have_no_state_for_is_skipped(self) -> None:
        """
        Meta reports "played" for voice notes. Recording a status the rest of
        the application cannot act on would put noise in the audit log.
        """
        statuses, _ = MetaWhatsAppProvider().parse_webhook(self.status_payload(status="played"))
        assert statuses == []

    def test_a_failed_status_carries_the_provider_error(self) -> None:
        payload = self.status_payload(
            status="failed",
            errors=[
                {
                    "code": 131026,
                    "title": "Message undeliverable",
                    "error_data": {"details": "Receiver is not a WhatsApp user"},
                }
            ],
        )

        statuses, _ = MetaWhatsAppProvider().parse_webhook(payload)

        assert statuses[0].error_code == "131026"
        assert statuses[0].error_message == "Receiver is not a WhatsApp user"

    def test_an_inbound_message_is_extracted(self) -> None:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": [{"wa_id": "9779800000001"}],
                                "messages": [
                                    {
                                        "from": "9779800000001",
                                        "id": "wamid.INBOUND",
                                        "timestamp": "1749416383",
                                        "type": "text",
                                        "text": {"body": "STOP"},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

        statuses, messages = MetaWhatsAppProvider().parse_webhook(payload)

        assert statuses == []
        assert messages[0].from_phone_number == "9779800000001"
        assert messages[0].text == "STOP"

    def test_several_entries_and_changes_are_all_read(self) -> None:
        """One delivery can batch many events; taking only the first loses the rest."""
        one = self.status_payload(id="wamid.A")
        two = self.status_payload(id="wamid.B")
        payload = {"object": "whatsapp_business_account", "entry": one["entry"] + two["entry"]}

        statuses, _ = MetaWhatsAppProvider().parse_webhook(payload)

        assert {s.provider_message_id for s in statuses} == {"wamid.A", "wamid.B"}

    @pytest.mark.parametrize("payload", [{}, {"entry": []}, {"entry": [{"changes": []}]}])
    def test_an_empty_payload_is_not_an_error(self, payload: dict) -> None:
        assert MetaWhatsAppProvider().parse_webhook(payload) == ([], [])


class TestFetchTemplates:
    def meta_template(self, **overrides) -> dict:
        template = {
            "name": "order_ready",
            "language": "en_US",
            "status": "APPROVED",
            "category": "UTILITY",
            "id": "1667192013751005",
            "components": [
                {"type": "HEADER", "text": "Your order"},
                {"type": "BODY", "text": "Good news {{1}}! Order #{{2}} is ready."},
                {"type": "FOOTER", "text": "Reply STOP to opt out"},
            ],
        }
        template.update(overrides)
        return template

    def test_a_template_is_mirrored_faithfully(self, http) -> None:
        http.add(responses.GET, TEMPLATES_URL, json={"data": [self.meta_template()]}, status=200)

        template = MetaWhatsAppProvider().fetch_templates()[0]

        assert template.name == "order_ready"
        assert template.status == "approved"
        assert template.category == "utility"
        assert template.body_text == "Good news {{1}}! Order #{{2}} is ready."
        assert template.header_text == "Your order"
        assert template.footer_text == "Reply STOP to opt out"
        assert template.provider_template_id == "1667192013751005"

    @pytest.mark.parametrize(
        ("meta_status", "expected"),
        [
            ("APPROVED", "approved"),
            ("PENDING", "pending"),
            ("IN_APPEAL", "pending"),
            ("REJECTED", "rejected"),
            ("PAUSED", "paused"),
            ("DISABLED", "disabled"),
            ("LIMIT_EXCEEDED", "paused"),
        ],
    )
    def test_approval_states_are_mapped(self, http, meta_status, expected) -> None:
        http.add(
            responses.GET,
            TEMPLATES_URL,
            json={"data": [self.meta_template(status=meta_status)]},
            status=200,
        )

        assert MetaWhatsAppProvider().fetch_templates()[0].status == expected

    def test_an_unrecognised_state_is_never_treated_as_approved(self, http) -> None:
        """
        Meta adds states over time. The safe reading of one we do not know is
        "do not send with this".
        """
        http.add(
            responses.GET,
            TEMPLATES_URL,
            json={"data": [self.meta_template(status="SOME_NEW_STATE")]},
            status=200,
        )

        assert MetaWhatsAppProvider().fetch_templates()[0].status == "disabled"

    def test_paging_is_followed(self, http) -> None:
        http.add(
            responses.GET,
            TEMPLATES_URL,
            json={
                "data": [self.meta_template(name="first")],
                "paging": {"cursors": {"after": "CURSOR"}},
            },
            status=200,
        )
        http.add(responses.GET, TEMPLATES_URL, json={"data": [self.meta_template(name="second")]})

        names = [t.name for t in MetaWhatsAppProvider().fetch_templates()]

        assert names == ["first", "second"]

    def test_an_error_response_raises_rather_than_reporting_zero_templates(self, http) -> None:
        """"0 templates" would read as "you have none", which is a different claim."""
        from core.exceptions import ProviderError

        http.add(responses.GET, TEMPLATES_URL, json=meta_error(190), status=401)

        with pytest.raises(ProviderError):
            MetaWhatsAppProvider().fetch_templates()

    def test_sync_without_the_waba_id_is_refused_by_name(self, settings) -> None:
        settings.META_WABA_ID = ""
        with pytest.raises(ProviderNotConfigured, match="WABA id"):
            MetaWhatsAppProvider().fetch_templates()
