"""
Meta WhatsApp Business Platform Cloud API provider.

Written against Meta's official documentation, September 2026:

* Send:      ``POST https://graph.facebook.com/{version}/{phone-number-id}/messages``
* Templates: ``GET  https://graph.facebook.com/{version}/{waba-id}/message_templates``
* Webhooks:  ``X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body, keyed by the app secret>``

Three decisions worth knowing before reading the code.

**Retryability is decided by Meta's numeric error code, not by the HTTP
status.** Meta's own guidance is to "build error handling around the ``code``
and ``details`` properties rather than message titles or HTTP status codes",
and the codes do not line up with the statuses — a rate limit and a permanently
undeliverable number can both arrive as an HTTP 400. :data:`RETRYABLE_CODES` is
the whole policy, in one place.

**Retrying is never a way to get past a limit.** Codes that mean "you are
sending too much" are retried on the provider's timetable and no faster;
per-recipient limits such as 131049 are treated as *permanent* precisely
because Meta documents that retrying them lowers your delivery rate without
changing the outcome.

**No credential is ever logged.** The access token lives only in the
Authorization header this module builds; error paths log Meta's code and
fbtrace_id, never the request headers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import requests
from django.conf import settings

from core.exceptions import ProviderNotConfigured
from whatsapp.services.base import (
    InboundMessage,
    InboundStatus,
    SendResult,
    TemplateData,
    WhatsAppProvider,
)

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.facebook.com"

# Settings that must be present for the *environment-configured* provider.
# A provider built from a customer's messaging account is checked against that
# account instead - see MetaWhatsAppProvider.check_configuration.
REQUIRED_SETTINGS = (
    "META_API_VERSION",
    "META_ACCESS_TOKEN",
    "META_PHONE_NUMBER_ID",
)

# Meta error codes worth another attempt. Everything not listed here is treated
# as permanent: retrying a permanent failure burns quota and, for the
# per-recipient limits, actively depresses the delivery rate Meta scores us on.
#
#     4        app-level API call rate limit
#     80007    the WABA has reached its rate limit
#     130429   Cloud API throughput reached
#     131000   unknown error, Meta advises retrying
#     131016   a service is temporarily unavailable
#     131056   too many messages to the same recipient in a short period
#     133004   server temporarily unavailable
#     133016   registration attempt limit exceeded
#     2494100  account in maintenance mode
RETRYABLE_CODES = frozenset({"4", "80007", "130429", "131000", "131016", "131056", "133004", "133016", "2494100"})

# Deliberately *not* retryable even though they look transient. Meta documents
# that repeated retries here "artificially lower your perceived delivery rate,
# as the same per-user limit may still be in effect resulting in the same
# outcome". Rule: throttle ourselves, never push against a limit.
NEVER_RETRY_CODES = frozenset({"131049", "131048"})

# Meta's template approval states, mapped onto ours. Anything unlisted is
# treated as not usable rather than guessed at — see _template_status.
TEMPLATE_STATUS_MAP = {
    "APPROVED": "approved",
    "PENDING": "pending",
    "IN_APPEAL": "pending",
    "PENDING_DELETION": "pending",
    "REJECTED": "rejected",
    "PAUSED": "paused",
    "DISABLED": "disabled",
    "DELETED": "disabled",
    "ARCHIVED": "disabled",
    "LIMIT_EXCEEDED": "paused",
}

TEMPLATE_CATEGORY_MAP = {
    "MARKETING": "marketing",
    "UTILITY": "utility",
    "AUTHENTICATION": "authentication",
}

# Meta reports "played" for voice notes. We have no such state, and inventing
# one would put a status in the log that no part of the app can act on.
STATUS_MAP = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


class MetaWhatsAppProvider(WhatsAppProvider):
    """Sends through Meta's official Cloud API."""

    name = "meta"
    is_simulated = False

    def __init__(
        self,
        *,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        waba_id: str | None = None,
        account=None,
    ):
        """
        Credentials come from a customer's account, or from the environment.

        Passing them in is how Stage 5 made sending multi-tenant: a customer's
        campaign goes out on their number, against their messaging limit, using
        their token. The environment remains the fallback for a single-tenant
        installation, which is every deployment that existed before this.

        ``api_version`` is *not* per-account. One Meta App serves every tenant,
        and the Graph version is a property of that app rather than of anybody's
        WABA.
        """
        self.account = account
        self.api_version = getattr(settings, "META_API_VERSION", "")
        self.phone_number_id = phone_number_id or getattr(settings, "META_PHONE_NUMBER_ID", "")
        self.waba_id = waba_id or getattr(settings, "META_WABA_ID", "")
        self.timeout = getattr(settings, "WHATSAPP_REQUEST_TIMEOUT", 30)

        # Held on the instance, never re-read from settings, so that a provider
        # built for one tenant cannot end up sending with another's token.
        self._access_token = (
            access_token if access_token is not None
            else getattr(settings, "META_ACCESS_TOKEN", "")
        )

    # -- Configuration ------------------------------------------------------

    def check_configuration(self) -> None:
        """
        Fail by name, listing what is missing — but never printing a value.

        Checks the *instance*, not the settings: a provider built from a
        customer's account has to be judged on that account's credentials, and
        checking settings would pass an organization whose token is empty
        purely because the environment happens to have one.
        """
        missing = [
            name
            for name, value in (
                ("META_API_VERSION", self.api_version),
                ("access token", self._access_token),
                ("phone number id", self.phone_number_id),
            )
            if not value
        ]
        if missing:
            where = (
                f"messaging account {self.account.pk}"
                if self.account is not None
                else "the environment"
            )
            raise ProviderNotConfigured(
                "The Meta provider is missing required configuration: "
                f"{', '.join(missing)}. Set these in {where}."
            )

    # -- HTTP ---------------------------------------------------------------

    @property
    def _messages_url(self) -> str:
        return f"{GRAPH_BASE_URL}/{self.api_version}/{self.phone_number_id}/messages"

    def _headers(self) -> dict[str, str]:
        """The only place the access token is ever read."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _post(self, url: str, payload: dict) -> SendResult:
        """
        POST to the Graph API and translate whatever comes back into a result.

        Every failure mode ends as a ``SendResult`` rather than an exception,
        because the caller — one Celery task per recipient — needs a decision
        about retrying, not a traceback.
        """
        try:
            response = requests.post(
                url, json=payload, headers=self._headers(), timeout=self.timeout
            )
        except requests.Timeout:
            return SendResult.failure(
                "timeout",
                f"The provider did not respond within {self.timeout}s.",
                retryable=True,
            )
        except requests.RequestException as exc:
            # The class name is safe to surface; the exception's own string can
            # embed the full URL, and the URL carries the phone number id.
            return SendResult.failure(
                "network_error",
                f"Could not reach the provider ({exc.__class__.__name__}).",
                retryable=True,
            )

        body = _json_or_empty(response)

        if response.ok:
            return _success(body)

        return _failure(response, body)

    # -- Sending ------------------------------------------------------------

    def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language: str,
        body_variables: Sequence[str] = (),
        header_variables: Sequence[str] = (),
    ) -> SendResult:
        self.check_configuration()

        components: list[dict[str, Any]] = []
        if header_variables:
            components.append(
                {"type": "header", "parameters": [_text_parameter(v) for v in header_variables]}
            )
        if body_variables:
            components.append(
                {"type": "body", "parameters": [_text_parameter(v) for v in body_variables]}
            )

        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language or "en_US"},
        }
        # Meta rejects an empty components array, so it is omitted entirely
        # for a template that takes no variables.
        if components:
            template["components"] = components

        return self._post(
            self._messages_url,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": _recipient(to),
                "type": "template",
                "template": template,
            },
        )

    def send_text(self, *, to: str, body: str) -> SendResult:
        self.check_configuration()
        return self._post(
            self._messages_url,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": _recipient(to),
                "type": "text",
                "text": {"body": body},
            },
        )

    # -- Templates ----------------------------------------------------------

    def fetch_templates(self) -> list[TemplateData]:
        """
        Every template on the WABA, following Meta's cursor paging.

        Reports each template's status exactly as Meta gives it. Nothing here
        may decide a template is approved — that is Meta's judgement, and this
        method's whole job is to mirror it faithfully.
        """
        self.check_configuration()
        if not self.waba_id:
            raise ProviderNotConfigured(
                "Template sync requires a WABA id, on the messaging account or in "
                "the environment."
            )

        url = f"{GRAPH_BASE_URL}/{self.api_version}/{self.waba_id}/message_templates"
        params: dict[str, Any] = {"limit": 100}
        templates: list[TemplateData] = []
        # A WABA has tens of templates, not thousands. The cap stops a broken
        # cursor from looping forever rather than expressing a real limit.
        for _page in range(20):
            response = requests.get(
                url, params=params, headers=self._headers(), timeout=self.timeout
            )
            body = _json_or_empty(response)

            if not response.ok:
                raise _template_error(body)

            for entry in body.get("data") or []:
                templates.append(_template_from_meta(entry))

            after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (body.get("data") or []):
                break
            params = {"limit": 100, "after": after}

        return templates

    # -- Webhooks -----------------------------------------------------------

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """
        Whether ``raw_body`` genuinely came from Meta.

        The hash is over the *raw* bytes: re-serialising the parsed JSON would
        change the whitespace and key order and never match. Compared with
        :func:`hmac.compare_digest` so a wrong signature cannot be discovered
        one byte at a time.
        """
        secret = getattr(settings, "META_APP_SECRET", "")
        if not secret:
            raise ProviderNotConfigured(
                "Webhook verification requires META_APP_SECRET to be set."
            )

        if not signature_header:
            return False

        prefix, _, provided = signature_header.strip().partition("=")
        if prefix != "sha256" or not provided:
            return False

        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        # Compared as bytes: the str form of compare_digest raises TypeError on
        # non-ASCII, and this half comes from an attacker-controlled header.
        return hmac.compare_digest(expected.encode("ascii"), provided.encode("utf-8", "replace"))

    def parse_webhook(self, payload: dict) -> tuple[list[InboundStatus], list[InboundMessage]]:
        """
        Split a webhook payload into status updates and inbound messages.

        One delivery can carry several entries, each with several changes, each
        with both arrays — so this flattens rather than assuming one of
        anything. Unknown status values are skipped: Meta reports "played" for
        voice notes, and there is no honest way to record a state the rest of
        the application cannot act on.
        """
        statuses: list[InboundStatus] = []
        messages: list[InboundMessage] = []

        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}

                for raw in value.get("statuses") or []:
                    status = STATUS_MAP.get(raw.get("status"))
                    if status is None:
                        logger.debug("Ignoring unmapped webhook status %r", raw.get("status"))
                        continue

                    error = (raw.get("errors") or [{}])[0]
                    statuses.append(
                        InboundStatus(
                            provider_message_id=raw.get("id", ""),
                            status=status,
                            timestamp=_moment(raw.get("timestamp")),
                            error_code=str(error.get("code", "")),
                            error_message=_error_text(error),
                            raw=raw,
                        )
                    )

                # Which of our numbers this arrived on. One webhook URL serves
                # every tenant, so this is how an inbound message is attributed
                # to the right customer.
                received_on = (value.get("metadata") or {}).get("phone_number_id", "")

                for raw in value.get("messages") or []:
                    messages.append(
                        InboundMessage(
                            # Meta reports the sender without a leading "+";
                            # the caller normalises it before matching a contact.
                            from_phone_number=raw.get("from", ""),
                            text=((raw.get("text") or {}).get("body") or ""),
                            provider_message_id=raw.get("id", ""),
                            timestamp=_moment(raw.get("timestamp")),
                            business_phone_number_id=received_on,
                            raw=raw,
                        )
                    )

        return statuses, messages


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------


def _json_or_empty(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _success(body: dict) -> SendResult:
    messages = body.get("messages") or []
    wamid = messages[0].get("id", "") if messages else ""

    if not wamid:
        # A 200 with no id is not a send we can track: without a wamid no
        # webhook can ever be matched back to this message.
        return SendResult.failure(
            "no_message_id",
            "The provider accepted the request but returned no message id.",
            retryable=True,
            raw=body,
        )
    return SendResult.ok(wamid, raw=body)


def _failure(response: requests.Response, body: dict) -> SendResult:
    error = body.get("error") or {}
    code = str(error.get("code", "")) or f"http_{response.status_code}"
    detail = (error.get("error_data") or {}).get("details") or ""
    message = detail or error.get("message") or f"HTTP {response.status_code} from the provider."

    logger.warning(
        "Meta send failed: code=%s http=%s fbtrace_id=%s",
        code,
        response.status_code,
        error.get("fbtrace_id", ""),
    )

    return SendResult.failure(
        code,
        message[:255],
        retryable=_is_retryable(code, response.status_code),
        retry_after=_retry_after(response),
        raw=body,
    )


def _is_retryable(code: str, http_status: int) -> bool:
    """
    Whether another attempt could plausibly succeed.

    The numeric code decides, because Meta's codes and HTTP statuses do not
    agree — a throughput limit and a dead number both arrive as a 400. An HTTP
    5xx with no code at all is the one case where the status is all there is.
    """
    if code in NEVER_RETRY_CODES:
        return False
    if code in RETRYABLE_CODES:
        return True
    return code.startswith("http_") and http_status >= 500


def _retry_after(response: requests.Response) -> int | None:
    """
    Honour ``Retry-After`` when the provider sends one.

    Meta does not document sending this header, so it is read opportunistically
    rather than depended on; when it is absent the caller falls back to its own
    exponential backoff.
    """
    raw = response.headers.get("Retry-After", "")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def _template_error(body: dict):
    from core.exceptions import ProviderError

    error = body.get("error") or {}
    return ProviderError(
        error.get("message") or "The provider rejected the template request.",
        provider_code=str(error.get("code", "")),
    )


def _text_parameter(value: str) -> dict[str, str]:
    return {"type": "text", "text": str(value)}


def _recipient(phone_number: str) -> str:
    """
    Meta's examples give ``to`` without a leading "+", and that is also the
    form it echoes back in webhooks. We store E.164, so the plus comes off here
    — in one place, rather than at every call site.
    """
    return (phone_number or "").lstrip("+")


def _moment(timestamp: Any) -> datetime | None:
    """Meta sends Unix epoch seconds, as a string."""
    try:
        return datetime.fromtimestamp(int(timestamp), tz=UTC)
    except (TypeError, ValueError):
        return None


def _error_text(error: dict) -> str:
    detail = (error.get("error_data") or {}).get("details") or ""
    return (detail or error.get("title") or error.get("message") or "")[:255]


def _template_from_meta(entry: dict) -> TemplateData:
    components = entry.get("components") or []

    def component_text(kind: str) -> str:
        for component in components:
            if (component.get("type") or "").upper() == kind:
                return component.get("text") or ""
        return ""

    return TemplateData(
        name=entry.get("name", ""),
        language=entry.get("language", ""),
        category=TEMPLATE_CATEGORY_MAP.get((entry.get("category") or "").upper(), "utility"),
        status=_template_status(entry.get("status")),
        body_text=component_text("BODY"),
        header_text=component_text("HEADER"),
        footer_text=component_text("FOOTER"),
        provider_template_id=str(entry.get("id", "")),
        rejection_reason=entry.get("rejected_reason") or "",
    )


def _template_status(raw: str | None) -> str:
    """
    Map Meta's approval state onto ours.

    An unrecognised state becomes "disabled" rather than anything usable. Meta
    adds states over time, and the safe reading of a state we do not understand
    is "do not send with this", never "approved".
    """
    mapped = TEMPLATE_STATUS_MAP.get((raw or "").upper())
    if mapped is None:
        logger.warning("Unrecognised template status %r from Meta; treating as disabled.", raw)
        return "disabled"
    return mapped
