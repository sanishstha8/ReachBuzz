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
