"""
Message statistics and status transitions.

Status updates arrive out of order and can be redelivered, so
:func:`apply_status_update` is both monotonic and idempotent — the two
properties that make webhook handling in Phase 7 safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from messaging.models import (
    STATUS_RANK,
    Message,
    MessageStatus,
    MessageStatusEvent,
    StatusEventSource,
)

logger = logging.getLogger(__name__)

# Which timestamp column each status stamps.
STATUS_TIMESTAMP_FIELD = {
    MessageStatus.QUEUED: "queued_at",
    MessageStatus.SENT: "sent_at",
    MessageStatus.DELIVERED: "delivered_at",
    MessageStatus.READ: "read_at",
    MessageStatus.FAILED: "failed_at",
}


@dataclass
class CampaignStats:
    """Status breakdown for a campaign, as shown on its monitoring page."""

    total: int = 0
    pending: int = 0
    queued: int = 0
    sending: int = 0
    sent: int = 0
    delivered: int = 0
    read: int = 0
    failed: int = 0

    @property
    def in_flight(self) -> int:
        return self.pending + self.queued + self.sending

    @property
    def processed(self) -> int:
        """Messages that have reached a terminal state."""
        return self.sent + self.delivered + self.read + self.failed

    @property
    def reached(self) -> int:
        """Messages the provider accepted, whatever happened afterwards."""
        return self.sent + self.delivered + self.read

    @property
    def progress_percent(self) -> float:
        if not self.total:
            return 0.0
        return round(self.processed / self.total * 100, 1)

    @property
    def delivery_rate(self) -> float:
        if not self.reached:
            return 0.0
        return round((self.delivered + self.read) / self.reached * 100, 1)

    @property
    def failure_rate(self) -> float:
        if not self.total:
            return 0.0
        return round(self.failed / self.total * 100, 1)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "total": self.total,
            "pending": self.pending,
            "queued": self.queued,
            "sending": self.sending,
            "sent": self.sent,
            "delivered": self.delivered,
            "read": self.read,
            "failed": self.failed,
            "in_flight": self.in_flight,
            "processed": self.processed,
            "progress_percent": self.progress_percent,
            "delivery_rate": self.delivery_rate,
            "failure_rate": self.failure_rate,
        }


def campaign_stats(campaign) -> CampaignStats:
    """Status counts for one campaign, in a single query."""
    counts = Message.objects.filter(campaign=campaign).aggregate(
        total=Count("id"),
        **{
            f"{status}_count": Count("id", filter=Q(status=status))
            for status, _ in MessageStatus.choices
        },
    )
    return CampaignStats(
        total=counts["total"],
        pending=counts["pending_count"],
        queued=counts["queued_count"],
        sending=counts["sending_count"],
        sent=counts["sent_count"],
        delivered=counts["delivered_count"],
        read=counts["read_count"],
        failed=counts["failed_count"],
    )


def global_stats(organization) -> dict[str, int]:
    """
    Message counts across one organization's campaigns, for the dashboard tiles.

    Takes the organization rather than defaulting to all of them: a tile that
    silently counted every tenant's messages would be a leak wearing the
    disguise of a number.
    """
    counts = Message.objects.for_organization(organization).aggregate(
        total=Count("id"),
        **{
            f"{status}_count": Count("id", filter=Q(status=status))
            for status, _ in MessageStatus.choices
        },
    )
    reached = counts["sent_count"] + counts["delivered_count"] + counts["read_count"]
    return {
        "total": counts["total"],
        "sent": reached,
        "delivered": counts["delivered_count"] + counts["read_count"],
        "read": counts["read_count"],
        "failed": counts["failed_count"],
        "pending": counts["pending_count"] + counts["queued_count"] + counts["sending_count"],
    }


@dataclass(frozen=True)
class FailureReason:
    """A distinct provider error and how often it occurred."""

    error_code: str
    error_message: str
    count: int
    affected_campaigns: int

    @property
    def label(self) -> str:
        return self.error_message or self.error_code or "Unknown error"


def failure_breakdown(queryset, *, limit: int = 20, per_campaign: bool = False) -> list[FailureReason]:
    """
    Group failed messages by the error the provider reported.

    Answers a question the list of failed messages cannot: whether a campaign
    hit one bad number or something systemic. Lives here rather than in the
    reporting app because it is a fact about messages, and both the campaign
    monitoring page and the reports page need the same answer.
    """
    aggregates: dict[str, Count] = {"count": Count("id")}
    if per_campaign:
        aggregates["affected_campaigns"] = Count("campaign", distinct=True)

    rows = (
        queryset.filter(status=MessageStatus.FAILED)
        .values("error_code", "error_message")
        .annotate(**aggregates)
        .order_by("-count", "error_code")[:limit]
    )
    return [
        FailureReason(
            error_code=row["error_code"],
            error_message=row["error_message"],
            count=row["count"],
            affected_campaigns=row.get("affected_campaigns", 1),
        )
        for row in rows
    ]


def campaign_failure_reasons(campaign, *, limit: int = 10) -> list[FailureReason]:
    """The failure breakdown for one campaign's monitoring page."""
    return failure_breakdown(Message.objects.filter(campaign=campaign), limit=limit)


@dataclass
class StatusUpdate:
    """A status report about one message, from a provider or from ourselves."""

    status: str
    provider_message_id: str = ""
    # Needs the annotation: without one this is a plain class attribute, not a
    # dataclass field, and the constructor silently refuses to accept it.
    provider_timestamp: datetime | None = None
    error_code: str = ""
    error_message: str = ""
    payload: dict = field(default_factory=dict)
    source: str = StatusEventSource.WEBHOOK


@transaction.atomic
def apply_status_update(message: Message, update: StatusUpdate) -> bool:
    """
    Apply a status report to ``message``.

    Returns True when the message's own status advanced. Two guarantees:

    * **Idempotent** — the unique constraint on (message, status, timestamp)
      means a redelivered webhook is recorded once and changes nothing twice.
    * **Monotonic** — a late "sent" callback arriving after "read" is stored in
      the event log but never drags the message's status backwards.
    """
    try:
        with transaction.atomic():
            MessageStatusEvent.objects.create(
                message=message,
                status=update.status,
                source=update.source,
                provider_timestamp=update.provider_timestamp,
                error_code=update.error_code[:32],
                error_message=update.error_message[:255],
                payload=update.payload or {},
            )
    except IntegrityError:
        logger.debug(
            "Duplicate status event ignored for message %s (%s)", message.pk, update.status
        )
        return False

    if message.outranks(update.status):
        logger.debug(
            "Ignoring out-of-order %s for message %s already at %s",
            update.status,
            message.pk,
            message.status,
        )
        return False

    update_fields = ["status", "updated_at"]
    message.status = update.status

    timestamp_field = STATUS_TIMESTAMP_FIELD.get(update.status)
    if timestamp_field and getattr(message, timestamp_field) is None:
        setattr(message, timestamp_field, update.provider_timestamp or timezone.now())
        update_fields.append(timestamp_field)

    if update.provider_message_id and not message.provider_message_id:
        message.provider_message_id = update.provider_message_id
        update_fields.append("provider_message_id")

    if update.status == MessageStatus.FAILED:
        message.error_code = update.error_code[:32]
        message.error_message = update.error_message[:255]
        message.error_details = update.payload or {}
        update_fields += ["error_code", "error_message", "error_details"]

    message.save(update_fields=update_fields)
    return True


def release_claim(message: Message, *, to_status: str = MessageStatus.QUEUED) -> None:
    """
    Hand a claimed message back so it can be picked up again.

    Used when a send is deferred rather than attempted — the campaign was
    paused, or the rate limiter said wait. The row must not be left in SENDING,
    or nothing would ever claim it again.
    """
    Message.objects.filter(pk=message.pk, status=MessageStatus.SENDING).update(
        status=to_status, updated_at=timezone.now()
    )
    message.status = to_status


def record_send_success(message: Message, provider_message_id: str, raw: dict | None = None) -> None:
    """Record that the provider accepted the message."""
    apply_status_update(
        message,
        StatusUpdate(
            status=MessageStatus.SENT,
            provider_message_id=provider_message_id,
            provider_timestamp=timezone.now(),
            payload=raw or {},
            source=StatusEventSource.SEND_RESPONSE,
        ),
    )


def record_send_failure(
    message: Message,
    *,
    error_code: str,
    error_message: str,
    attempt: int,
    raw: dict | None = None,
) -> None:
    """Record a permanent (or finally-exhausted) send failure."""
    message.attempt_count = attempt
    message.save(update_fields=["attempt_count", "updated_at"])

    apply_status_update(
        message,
        StatusUpdate(
            status=MessageStatus.FAILED,
            provider_timestamp=timezone.now(),
            error_code=error_code,
            error_message=error_message,
            payload=raw or {},
            source=StatusEventSource.SEND_RESPONSE,
        ),
    )


def schedule_retry(message: Message, *, attempt: int, delay_seconds: int) -> None:
    """Put a message back in the queue for another attempt later."""
    message.attempt_count = attempt
    message.next_retry_at = timezone.now() + timedelta(seconds=delay_seconds)
    message.status = MessageStatus.QUEUED
    message.save(
        update_fields=["attempt_count", "next_retry_at", "status", "updated_at"]
    )


def claim_for_sending(message_id) -> Message | None:
    """
    Atomically claim a message for sending.

    A conditional UPDATE is the whole idempotency mechanism: if two workers
    receive the same job, exactly one sees ``rowcount == 1`` and proceeds. The
    other finds nothing to claim and stops.
    """
    from messaging.models import CLAIMABLE_STATUSES

    claimed = Message.objects.filter(pk=message_id, status__in=CLAIMABLE_STATUSES).update(
        status=MessageStatus.SENDING, updated_at=timezone.now()
    )
    if claimed != 1:
        return None
    return Message.objects.select_related("campaign", "contact", "template").get(pk=message_id)


def status_rank(status: str) -> int:
    return STATUS_RANK.get(status, 0)
