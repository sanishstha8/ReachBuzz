"""
Rolling billing periods forward.

A subscription's period has to end even when nobody is looking, because the
monthly message quota resets with it. If this never ran, a customer who hit
their ceiling in March would stay blocked in April — the limit is measured
against ``current_period_start``, and nothing else moves it.

**Safe to run twice, and safe to run late.** The snapshot is written under a
unique constraint on (organization, period_start), so a retry cannot bill a
period twice. A subscription several periods behind — the machine was off, the
worker was down — is rolled forward one period at a time until it catches up,
rather than jumping straight to now and losing the periods in between along
with their usage.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from billing.usage import close_period, due_for_renewal

logger = logging.getLogger(__name__)

#: A guard against an unbounded loop if a period end were ever left in the past
#: by bad data. Two years of monthly periods is far more than a real backlog.
MAX_CATCH_UP_PERIODS = 24


@shared_task(name="billing.tasks.roll_billing_periods", ignore_result=True)
def roll_billing_periods() -> dict:
    """
    Close every period that has run out, and open the next one.

    Returns counts rather than raising on a single bad subscription: one
    customer with unusual data must not stop every other customer's quota from
    resetting.
    """
    now = timezone.now()
    closed = 0
    failed = 0

    for subscription in due_for_renewal(now):
        try:
            catch_up = 0
            while subscription.current_period_end <= now and catch_up < MAX_CATCH_UP_PERIODS:
                _, created = close_period(subscription)
                closed += int(created)
                catch_up += 1
                subscription.refresh_from_db()
                if not subscription.is_entitled:
                    break  # A cancellation took effect; stop rolling it forward.
        except Exception:
            failed += 1
            logger.exception(
                "Could not roll the billing period for organization %s",
                subscription.organization_id,
            )

    if closed or failed:
        logger.info("Rolled %s billing period(s) forward, %s failed", closed, failed)
    return {"closed": closed, "failed": failed}


@shared_task(name="billing.tasks.collect_due_invoices", ignore_result=True)
def collect_due_invoices() -> dict:
    """
    Attempt payment on every open invoice that has come due.

    Deliberately dull. Each invoice is collected independently, a failure on one
    is logged and skipped rather than raised, and `collect()` returns None for
    anything already settled — so a sweep that runs twice in a minute does
    nothing the second time.
    """
    from billing.invoicing import Invoice
    from billing.payments import collect

    attempted = 0
    settled = 0
    failed = 0

    for invoice in Invoice.objects.overdue().select_related("plan", "organization", "subscription"):
        try:
            payment = collect(invoice)
        except Exception:
            failed += 1
            logger.exception("Could not collect invoice %s", invoice.number)
            continue

        if payment is None:
            continue
        attempted += 1
        settled += int(payment.status == "succeeded")

    if attempted or failed:
        logger.info("Collected %s of %s due invoice(s), %s errored", settled, attempted, failed)
    return {"attempted": attempted, "settled": settled, "failed": failed}


@shared_task(name="billing.tasks.process_payment_webhook", ignore_result=True)
def process_payment_webhook(event_id: str) -> str:
    """
    Interpret one stored webhook event.

    The endpoint stores and answers 200; this does the work. Same split as the
    WhatsApp webhook, for the same reason — a provider retries a non-200 for
    days, and with money involved every retry is another chance to credit the
    same payment twice.
    """
    from billing.invoicing import PaymentWebhookEvent, PaymentWebhookStatus
    from billing.payments import apply_event
    from billing.providers.factory import get_provider

    event = PaymentWebhookEvent.objects.filter(pk=event_id).first()
    if event is None:
        logger.warning("Payment webhook event %s vanished before processing", event_id)
        return "missing"
    if event.status == PaymentWebhookStatus.PROCESSED:
        return "already-processed"

    try:
        # A loop rather than any(): every event in the payload must be applied,
        # and any() over a generator stops at the first True.
        changed = False
        for parsed in get_provider(event.provider).parse_webhook(event.payload):
            changed |= apply_event(parsed)
    except Exception as exc:
        event.status = PaymentWebhookStatus.FAILED
        event.error_message = str(exc)[:255]
        event.save(update_fields=["status", "error_message", "updated_at"])
        logger.exception("Could not process payment webhook %s", event_id)
        raise

    event.status = PaymentWebhookStatus.PROCESSED if changed else PaymentWebhookStatus.IGNORED
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at", "updated_at"])
    return event.status
