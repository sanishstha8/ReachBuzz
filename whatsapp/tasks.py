"""
Background sending.

One Celery task per recipient. That granularity is what makes a 1,000-message
campaign safe: a single failure retries on its own, a pause takes effect on the
next message rather than mid-batch, and a duplicated job is a no-op.

Correctness rests on three things, none of which involve trusting the queue:

* **Idempotency** — every send starts by *claiming* the row with a conditional
  UPDATE. If two workers get the same job, exactly one claim succeeds.
* **Ordering** — dispatch happens on ``transaction.on_commit``, so a worker can
  never see a message row that is not yet committed.
* **Backpressure** — a shared token bucket throttles us to the configured rate,
  and a provider ``Retry-After`` is honoured rather than worked around.
"""

from __future__ import annotations

import logging
import random

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from campaigns.models import Campaign, CampaignMessageType, CampaignStatus
from campaigns.services import finalize_if_complete
from messaging.models import CLAIMABLE_STATUSES, Message, MessageStatus, StatusEventSource
from messaging.services import (
    StatusUpdate,
    apply_status_update,
    claim_for_sending,
    record_send_failure,
    record_send_success,
    release_claim,
    schedule_retry,
)
from whatsapp.services.factory import get_provider, is_simulated
from whatsapp.services.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)

# Statuses in which a campaign's workers should stop doing anything.
HALTED_STATUSES = frozenset(
    {CampaignStatus.PAUSED, CampaignStatus.CANCELLED, CampaignStatus.FAILED}
)


# ---------------------------------------------------------------------------
# Campaign dispatch
# ---------------------------------------------------------------------------


@shared_task(name="whatsapp.tasks.dispatch_campaign", ignore_result=True)
def dispatch_campaign_task(campaign_id: str) -> int:
    """Queue a send task for every message of this campaign not yet handled."""
    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if campaign is None:
        logger.warning("dispatch_campaign: campaign %s no longer exists", campaign_id)
        return 0

    if campaign.status in HALTED_STATUSES:
        logger.info(
            "dispatch_campaign: campaign %s is %s; nothing queued", campaign_id, campaign.status
        )
        return 0

    message_ids = list(
        Message.objects.filter(campaign=campaign, status__in=CLAIMABLE_STATUSES)
        .order_by("created_at")
        .values_list("id", flat=True)
    )

    for message_id in message_ids:
        send_message_task.delay(str(message_id))

    Message.objects.filter(campaign=campaign, status=MessageStatus.PENDING).update(
        status=MessageStatus.QUEUED, queued_at=timezone.now(), updated_at=timezone.now()
    )

    logger.info("dispatch_campaign: queued %d message(s) for campaign %s", len(message_ids), campaign_id)

    # A campaign whose messages are all already terminal (a re-dispatch after
    # everything finished) should not sit in PROCESSING forever.
    if not message_ids:
        finalize_if_complete(campaign)

    return len(message_ids)


def queue_campaign(campaign: Campaign) -> int:
    """
    The dispatcher registered with ``campaigns.dispatch``.

    Kept as a thin function so the campaign services stay free of any Celery
    import and remain testable without a broker.
    """
    dispatch_campaign_task.delay(str(campaign.pk))
    return Message.objects.filter(campaign=campaign, status__in=CLAIMABLE_STATUSES).count()


def preflight() -> None:
    """
    Refuse a launch when the queue could not accept the work.

    Without this a campaign would transition to PROCESSING, the on-commit
    dispatch would fail against an unreachable broker, and every message would
    sit at PENDING with nothing to move it.
    """
    from campaigns.dispatch import SendingUnavailable

    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return

    from config.celery import app

    try:
        connection = app.connection()
        connection.ensure_connection(max_retries=0, timeout=3)
        connection.release()
    except Exception as exc:
        logger.warning("Queue broker unreachable: %s", exc.__class__.__name__)
        raise SendingUnavailable(
            "The message queue is unreachable, so this campaign cannot be sent yet. "
            "Check that Redis is running and CELERY_BROKER_URL is correct."
        ) from exc


# Advertised on the dispatcher so campaigns.dispatch.preflight() can find it.
queue_campaign.preflight = preflight


# ---------------------------------------------------------------------------
# Sending one message
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    name="whatsapp.tasks.send_message",
    ignore_result=True,
    acks_late=True,
)
def send_message_task(self, message_id: str) -> str:
    """
    Send one message.

    Returns a short outcome string, which is what the tests assert on and what
    appears in the worker log.
    """
    message = claim_for_sending(message_id)
    if message is None:
        # Someone else claimed it, or it already reached a terminal state.
        # This is the normal, expected outcome of a duplicated job.
        logger.debug("send_message: %s was already handled", message_id)
        return "already-handled"

    campaign = message.campaign

    if campaign.status in HALTED_STATUSES:
        release_claim(message, to_status=MessageStatus.QUEUED)
        logger.info("send_message: campaign %s is %s; deferring", campaign.pk, campaign.status)
        return f"campaign-{campaign.status}"

    # --- backpressure ------------------------------------------------------
    acquisition = get_rate_limiter().acquire()
    if not acquisition.allowed:
        release_claim(message, to_status=MessageStatus.QUEUED)
        raise self.retry(countdown=acquisition.retry_after, max_retries=None)

    # --- send --------------------------------------------------------------
    provider = get_provider()
    attempt = message.attempt_count + 1

    try:
        result = _send(provider, message)
    except NotImplementedError as exc:
        # A provider that is configured but not implemented is a deployment
        # mistake, not a per-message failure: do not retry it 1,000 times.
        release_claim(message, to_status=MessageStatus.QUEUED)
        logger.error("send_message: provider %s cannot send: %s", provider.name, exc)
        return "provider-not-implemented"
    except Exception as exc:
        logger.exception("send_message: unexpected error sending %s", message_id)
        return _handle_failure(
            self,
            message,
            attempt=attempt,
            error_code="internal_error",
            error_message=f"Unexpected error: {exc.__class__.__name__}",
            retryable=True,
            retry_after=None,
        )

    if result.success:
        record_send_success(message, result.provider_message_id, result.raw)
        logger.info("send_message: %s sent as %s", message_id, result.provider_message_id)

        if is_simulated() and getattr(settings, "MOCK_PROVIDER_SIMULATE_CALLBACKS", False):
            simulate_status_callbacks_task.apply_async(
                args=[str(message.pk)],
                countdown=getattr(settings, "MOCK_PROVIDER_CALLBACK_DELAY", 3),
            )

        _finalize(campaign)
        return "sent"

    return _handle_failure(
        self,
        message,
        attempt=attempt,
        error_code=result.error_code,
        error_message=result.error_message,
        retryable=result.retryable,
        retry_after=result.retry_after,
        raw=result.raw,
    )


def _send(provider, message: Message):
    """Translate a Message into a provider call."""
    if message.message_type == CampaignMessageType.TEXT:
        return provider.send_text(
            to=message.to_phone_number,
            body=(message.rendered_payload or {}).get("text", ""),
        )

    values = (message.rendered_payload or {}).get("values", {})
    template = message.template
    ordered_tokens = list(template.variables or []) if template else list(values)

    return provider.send_template(
        to=message.to_phone_number,
        template_name=message.template_name,
        language=message.template_language,
        body_variables=[str(values.get(token, "")) for token in ordered_tokens],
    )


def _handle_failure(
    task,
    message: Message,
    *,
    attempt: int,
    error_code: str,
    error_message: str,
    retryable: bool,
    retry_after: int | None,
    raw: dict | None = None,
) -> str:
    """Retry a transient failure, or record a permanent one."""
    max_retries = getattr(settings, "WHATSAPP_MAX_RETRIES", 3)

    if retryable and attempt <= max_retries:
        delay = retry_after or _backoff(attempt)
        schedule_retry(message, attempt=attempt, delay_seconds=delay)
        logger.info(
            "send_message: %s failed (%s), retry %d/%d in %ds",
            message.pk,
            error_code,
            attempt,
            max_retries,
            delay,
        )
        raise task.retry(countdown=delay, max_retries=max_retries)

    if retryable:
        error_message = f"{error_message} (gave up after {max_retries} retries)"

    record_send_failure(
        message,
        error_code=error_code,
        error_message=error_message,
        attempt=attempt,
        raw=raw,
    )
    _record_contact_error(message, error_code, error_message)
    _finalize(message.campaign)
    logger.warning("send_message: %s permanently failed (%s)", message.pk, error_code)
    return "failed"


def _backoff(attempt: int) -> int:
    """
    Exponential backoff with jitter.

    The jitter matters: without it, a thousand messages that failed together
    would all retry at the same instant and fail together again.
    """
    base = getattr(settings, "WHATSAPP_RETRY_BACKOFF_SECONDS", 10)
    delay = base * (2 ** (attempt - 1))
    # Jitter, not cryptography: spreading retries is the whole point.
    return int(min(delay, 3600) * random.uniform(0.8, 1.2)) or 1  # noqa: S311


def _record_contact_error(message: Message, error_code: str, error_message: str) -> None:
    """Surface the last delivery problem on the contact, for the operator."""
    from contacts.models import Contact

    Contact.objects.filter(pk=message.contact_id).update(
        last_error_code=error_code[:32],
        last_error_message=error_message[:255],
        updated_at=timezone.now(),
    )


def _finalize(campaign: Campaign) -> None:
    campaign.refresh_from_db()
    finalize_if_complete(campaign)


# ---------------------------------------------------------------------------
# Mock delivery simulation
# ---------------------------------------------------------------------------


@shared_task(name="whatsapp.tasks.simulate_status_callbacks", ignore_result=True)
def simulate_status_callbacks_task(message_id: str) -> str:
    """
    Stand in for the provider's delivery webhooks while running on the mock.

    Recorded with ``StatusEventSource.SIMULATED`` so a simulated delivery can
    never be mistaken for one a real provider reported.
    """
    if not is_simulated():
        return "not-simulated"

    message = Message.objects.filter(pk=message_id).first()
    if message is None or message.status != MessageStatus.SENT:
        return "skipped"

    rng = random.Random(str(message_id))  # noqa: S311 - simulation only
    now = timezone.now()

    apply_status_update(
        message,
        StatusUpdate(
            status=MessageStatus.DELIVERED,
            provider_timestamp=now,
            source=StatusEventSource.SIMULATED,
            payload={"simulated": True},
        ),
    )

    read_rate = getattr(settings, "MOCK_PROVIDER_READ_RATE", 0.6)
    if rng.random() < read_rate:
        apply_status_update(
            message,
            StatusUpdate(
                status=MessageStatus.READ,
                provider_timestamp=now,
                source=StatusEventSource.SIMULATED,
                payload={"simulated": True},
            ),
        )
        return "read"

    return "delivered"


# ---------------------------------------------------------------------------
# Scheduled campaigns
# ---------------------------------------------------------------------------


@shared_task(name="whatsapp.tasks.run_due_campaigns", ignore_result=True)
def run_due_campaigns_task() -> int:
    """
    Launch campaigns whose scheduled time has arrived.

    Run by Celery beat. Each launch goes through the same
    ``campaigns.services.launch_campaign`` as a manual one, so validation,
    consent filtering and auditing are identical.
    """
    from campaigns.services import launch_campaign
    from core.exceptions import DomainError

    due = Campaign.objects.filter(
        status=CampaignStatus.SCHEDULED, scheduled_at__lte=timezone.now()
    )

    launched = 0
    for campaign in due:
        try:
            with transaction.atomic():
                launch_campaign(campaign)
        except DomainError as exc:
            campaign.status = CampaignStatus.FAILED
            campaign.failure_reason = exc.message[:255]
            campaign.save(update_fields=["status", "failure_reason", "updated_at"])
            logger.warning("Scheduled campaign %s could not launch: %s", campaign.pk, exc.message)
        else:
            launched += 1
            logger.info("Scheduled campaign %s launched", campaign.pk)

    return launched


# ---------------------------------------------------------------------------
# Inbound webhooks
# ---------------------------------------------------------------------------


@shared_task(name="whatsapp.tasks.process_webhook_event", ignore_result=True)
def process_webhook_event_task(event_id: str) -> str:
    """
    Interpret one stored webhook delivery.

    The endpoint has already answered 200, so nothing here can ask Meta to try
    again — which is the point. A failure is recorded on the event and left for
    the sweep, rather than being signalled upstream where it would earn a week
    of duplicate deliveries for a bug on our side.
    """
    from whatsapp.models import WebhookEvent, WebhookEventStatus
    from whatsapp.services.inbound import process_event

    event = WebhookEvent.objects.filter(pk=event_id).first()
    if event is None:
        logger.warning("process_webhook_event: event %s no longer exists", event_id)
        return "missing"

    if event.status == WebhookEventStatus.PROCESSED:
        # Meta redelivers, and so does our own sweep. Both are expected.
        return "already-processed"

    try:
        result = process_event(event)
    except Exception as exc:
        logger.exception("process_webhook_event: %s could not be processed", event_id)
        event.status = WebhookEventStatus.FAILED
        event.error_message = f"{exc.__class__.__name__}: {exc}"[:255]
        event.save(update_fields=["status", "error_message", "updated_at"])
        return "failed"

    event.status = WebhookEventStatus.PROCESSED
    event.processed_at = timezone.now()
    event.status_count = result.total_statuses
    event.message_count = result.messages_received
    event.error_message = ""
    event.save(
        update_fields=[
            "status",
            "processed_at",
            "status_count",
            "message_count",
            "error_message",
            "updated_at",
        ]
    )

    logger.info(
        "process_webhook_event: %s applied %d status(es), %d unmatched, %d inbound, %d opt-out(s)",
        event_id,
        result.statuses_applied,
        result.statuses_unmatched,
        result.messages_received,
        result.opt_outs,
    )
    return "processed"


@shared_task(name="whatsapp.tasks.process_pending_webhooks", ignore_result=True)
def process_pending_webhooks_task(limit: int = 100) -> int:
    """
    Pick up webhook events that were stored but never processed.

    The endpoint deliberately answers 200 once the payload is safely stored,
    even if queueing the follow-up task failed — so something has to notice
    those later. Without this sweep, a broker blip between the write and the
    enqueue would strand a delivery report silently and a campaign would sit at
    "processing" forever.
    """
    from whatsapp.models import WebhookEvent, WebhookEventStatus

    pending = list(
        WebhookEvent.objects.filter(status=WebhookEventStatus.RECEIVED)
        .order_by("created_at")
        .values_list("id", flat=True)[:limit]
    )

    for event_id in pending:
        process_webhook_event_task.delay(str(event_id))

    if pending:
        logger.info("process_pending_webhooks: requeued %d event(s)", len(pending))
    return len(pending)
