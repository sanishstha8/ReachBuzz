"""
Turning a stored webhook payload into state changes.

Kept out of the view and out of the task so the interesting part — what a
delivery report or a "STOP" actually does to our records — is testable without
a request or a broker.

Two properties come for free from work done earlier, and this module depends on
both. :func:`messaging.services.apply_status_update` is **idempotent** (a
redelivered webhook is recorded once and changes nothing twice) and
**monotonic** (a late "sent" arriving after "read" is logged but never drags
the message backwards). Meta redelivers for up to seven days and makes no
ordering promise, so without those two this would be a source of corruption
rather than of truth.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.utils import timezone

from messaging.models import Message, MessageStatus, StatusEventSource
from messaging.services import StatusUpdate, apply_status_update

logger = logging.getLogger(__name__)

# What a recipient can send to stop hearing from us. Matched against the whole
# message, not searched for inside it: "please don't stop sending these" must
# not opt somebody out, and a false positive here silently ends a conversation
# the customer wanted.
STOP_KEYWORDS = frozenset(
    {"stop", "stopall", "stop all", "unsubscribe", "cancel", "quit", "end", "optout", "opt out"}
)

_PUNCTUATION = re.compile(r"[^a-z ]+")


@dataclass
class ProcessResult:
    """What one webhook delivery actually changed."""

    statuses_applied: int = 0
    statuses_unmatched: int = 0
    messages_received: int = 0
    opt_outs: int = 0

    @property
    def total_statuses(self) -> int:
        return self.statuses_applied + self.statuses_unmatched


def is_stop_request(text: str) -> bool:
    """
    Whether an inbound message is a request to stop.

    Punctuation and case are stripped so "STOP." and "Stop" both count, but the
    comparison stays an equality test against the whole message.
    """
    cleaned = _PUNCTUATION.sub("", (text or "").casefold()).strip()
    return cleaned in STOP_KEYWORDS


def process_event(event) -> ProcessResult:
    """
    Apply one stored :class:`~whatsapp.models.WebhookEvent`.

    Returns a summary rather than raising on the ordinary cases — an unmatched
    status is normal, not an error, and one bad entry in a batch must not
    discard the rest of it.
    """
    from whatsapp.services.factory import get_provider

    statuses, messages = get_provider().parse_webhook(event.payload or {})
    result = ProcessResult()

    for status in statuses:
        if _apply_status(status):
            result.statuses_applied += 1
        else:
            result.statuses_unmatched += 1

    for message in messages:
        result.messages_received += 1
        if _handle_inbound(message):
            result.opt_outs += 1

    return result


# ---------------------------------------------------------------------------
# Delivery statuses
# ---------------------------------------------------------------------------


def _apply_status(status) -> bool:
    """
    Match a reported status back to our record of the message and apply it.

    ``provider_message_id`` is the join: it is stamped on the row when the
    provider accepts the send, and Meta quotes it back on every callback.
    """
    if not status.provider_message_id:
        logger.warning("Webhook status with no message id; ignoring.")
        return False

    message = Message.objects.filter(
        provider_message_id=status.provider_message_id
    ).select_related("campaign").first()

    if message is None:
        # Entirely normal: a message sent from WhatsApp Manager, or from
        # another system sharing this number, is not ours to record.
        logger.info("No local message matches %s; ignoring.", status.provider_message_id)
        return False

    changed = apply_status_update(
        message,
        StatusUpdate(
            status=status.status,
            provider_message_id=status.provider_message_id,
            provider_timestamp=status.timestamp or timezone.now(),
            error_code=status.error_code,
            error_message=status.error_message,
            payload=status.raw or {},
            source=StatusEventSource.WEBHOOK,
        ),
    )

    if changed and status.status == MessageStatus.FAILED:
        _record_contact_error(message, status.error_code, status.error_message)

    if changed:
        _finalize(message.campaign)

    return changed


def _record_contact_error(message: Message, error_code: str, error_message: str) -> None:
    """Surface the last delivery problem on the contact, for the operator."""
    from contacts.models import Contact

    Contact.objects.filter(pk=message.contact_id).update(
        last_error_code=(error_code or "")[:32],
        last_error_message=(error_message or "")[:255],
        updated_at=timezone.now(),
    )


def _finalize(campaign) -> None:
    """A campaign is finished when its last delivery report lands, not before."""
    from campaigns.services import finalize_if_complete

    campaign.refresh_from_db()
    finalize_if_complete(campaign)


# ---------------------------------------------------------------------------
# Inbound messages
# ---------------------------------------------------------------------------


def _handle_inbound(message) -> bool:
    """
    Act on a message a recipient sent us. Returns True if it opted them out.

    Only opting *out* is automatic. An inbound "START" is not treated as
    consent: consent is never inferred, and a keyword is a weaker basis than
    this system is willing to record. Opting back in stays a deliberate act by
    an operator, with a source and an audit entry behind it.
    """
    from contacts.models import Contact, OptOutSource
    from contacts.services import set_consent
    from core.phone import normalize_phone_number

    if not is_stop_request(message.text):
        return False

    contact = _find_contact(
        message.from_phone_number,
        normalize_phone_number,
        Contact,
        organization=_organization_for_inbound(message),
    )
    if contact is None:
        logger.info("STOP received from a number that is not a contact; nothing to opt out.")
        return False

    if not contact.opted_in:
        logger.info("STOP received from %s, who is already opted out.", contact.pk)
        return False

    # Through the service, so the withdrawal is timestamped, sourced and
    # audited exactly like one an operator makes by hand.
    set_consent(contact, opted_in=False, source=OptOutSource.INBOUND_STOP)
    logger.info("Contact %s opted out by inbound STOP.", contact.pk)
    return True


def _organization_for_inbound(message):
    """
    Whose customer sent this, decided by which of our numbers it arrived on.

    One webhook URL serves every tenant, and two customers can legitimately hold
    the same person as a contact. Matching on the sender's number alone would
    therefore withdraw consent from whichever organization's row happened to
    come back first — a consent bug, which is the one class of bug this
    project treats as unacceptable.

    Returns ``None`` when the receiving number is not one we have on file. The
    caller then falls back to an unscoped match, which is correct for a
    single-tenant installation — there is only one organization for the
    contact to belong to — and is logged, because on a platform it means a
    webhook arrived for a number nobody has connected.
    """
    from whatsapp.accounts import MessagingAccount

    received_on = getattr(message, "business_phone_number_id", "")
    if not received_on:
        return None

    account = (
        MessagingAccount.objects.filter(phone_number_id=received_on)
        .select_related("organization")
        .first()
    )
    if account is None:
        logger.warning(
            "Inbound message arrived on phone number id %s, which no messaging "
            "account claims.",
            received_on,
        )
        return None

    return account.organization


def _find_contact(raw_number: str, normalize, contact_model, *, organization=None):
    """
    Find the contact behind an inbound number, within one organization.

    Meta reports the sender without a leading "+", and we store E.164, so the
    number is normalised before matching rather than compared as text.
    """
    if not raw_number:
        return None

    candidates = [raw_number if raw_number.startswith("+") else f"+{raw_number}"]
    try:
        candidates.append(normalize(candidates[0]))
    except Exception:  # noqa: BLE001 - an unparseable number is just a miss
        logger.debug("Inbound number could not be normalised; matching on the raw form.")

    queryset = contact_model.objects.filter(phone_number__in=candidates)
    if organization is not None:
        queryset = queryset.filter(organization=organization)
    return queryset.first()
