"""
Subscription lifecycle: starting one, changing it, ending it.

Kept apart from :mod:`billing.usage`, which only ever *reads*. Everything that
writes a subscription goes through here, so the audit trail has one place to
live and a plan change cannot happen without one.

**No money changes hands in this module.** ``subscribe()`` records an
entitlement; it does not charge for it. Stage 4 puts a payment provider behind
these calls, and it can do that without touching them because the seam is
already here.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from billing.models import (
    Plan,
    Subscription,
    SubscriptionStatus,
    default_trial_end,
)
from core.audit import record_audit
from core.exceptions import ValidationFailed
from core.models import AuditAction

logger = logging.getLogger(__name__)


def default_plan() -> Plan | None:
    """
    What a brand-new organization starts on.

    The cheapest public plan, by the ordering the pricing page already uses, so
    that adding a cheaper tier moves new signups onto it without a code change.
    """
    return Plan.objects.public().first()


@transaction.atomic
def subscribe(
    organization,
    plan: Plan | None = None,
    *,
    status: str | None = None,
    user=None,
    request=None,
) -> Subscription:
    """
    Put an organization on a plan, starting a fresh period.

    Idempotent by ``OneToOneField``: calling it for an organization that already
    has a subscription changes that one rather than creating a second. Two rows
    disagreeing about a customer's limits is a state worth making impossible.
    """
    plan = plan or default_plan()
    if plan is None:
        raise ValidationFailed(
            "There are no plans to subscribe to.",
            details={"blockers": ["No active plan exists. Seed the plan catalogue first."]},
        )

    now = timezone.now()
    trial_end = default_trial_end(plan, now)
    if status is None:
        status = SubscriptionStatus.TRIALING if trial_end else SubscriptionStatus.ACTIVE

    subscription, created = Subscription.objects.update_or_create(
        organization=organization,
        defaults={
            "plan": plan,
            "status": status,
            "current_period_start": now,
            "current_period_end": None,  # save() fills it from the plan interval
            "trial_end": trial_end,
            "cancel_at_period_end": False,
            "canceled_at": None,
        },
    )

    record_audit(
        AuditAction.SUBSCRIPTION_STARTED if created else AuditAction.SUBSCRIPTION_CHANGED,
        user=user,
        request=request,
        obj=organization,
        description=f"{'Started' if created else 'Changed to'} the {plan.name} plan",
        metadata={"plan": plan.slug, "status": status},
    )
    logger.info("Organization %s subscribed to %s (%s)", organization.pk, plan.slug, status)
    return subscription


@transaction.atomic
def change_plan(subscription: Subscription, plan: Plan, *, user=None, request=None) -> Subscription:
    """
    Move an existing subscription to a different plan.

    **The period is not restarted.** Restarting it would hand a customer a fresh
    month of quota every time they switched plans, which is a free-messages
    exploit with an obvious recipe. The new limits apply to the usage already
    recorded in the period, which is also what makes an upgrade take effect
    immediately for somebody who has just hit a ceiling.
    """
    previous = subscription.plan

    if previous == plan:
        return subscription

    subscription.plan = plan
    if subscription.status == SubscriptionStatus.EXPIRED:
        subscription.status = SubscriptionStatus.ACTIVE
    subscription.cancel_at_period_end = False
    subscription.canceled_at = None
    subscription.save(
        update_fields=["plan", "status", "cancel_at_period_end", "canceled_at", "updated_at"]
    )

    record_audit(
        AuditAction.SUBSCRIPTION_CHANGED,
        user=user,
        request=request,
        obj=subscription.organization,
        description=f"Moved from {previous.name} to {plan.name}",
        metadata={"from": previous.slug, "to": plan.slug},
    )
    return subscription


@transaction.atomic
def cancel(
    subscription: Subscription, *, immediately: bool = False, user=None, request=None
) -> Subscription:
    """
    End a subscription, by default at the end of the period already paid for.

    Cutting a customer off the moment they click cancel takes away time they
    have already bought. ``immediately`` exists for the cases where that is the
    point — abuse, or a request to stop right now.
    """
    if immediately:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.canceled_at = timezone.now()
        subscription.cancel_at_period_end = False
        fields = ["status", "canceled_at", "cancel_at_period_end", "updated_at"]
    else:
        subscription.cancel_at_period_end = True
        fields = ["cancel_at_period_end", "updated_at"]

    subscription.save(update_fields=fields)

    record_audit(
        AuditAction.SUBSCRIPTION_CANCELLED,
        user=user,
        request=request,
        obj=subscription.organization,
        description=(
            "Cancelled immediately" if immediately else "Will cancel at the end of the period"
        ),
        metadata={"plan": subscription.plan.slug, "immediate": immediately},
    )
    return subscription


@transaction.atomic
def resume(subscription: Subscription, *, user=None, request=None) -> Subscription:
    """Undo a pending cancellation, while the period is still running."""
    if not subscription.cancel_at_period_end:
        return subscription

    subscription.cancel_at_period_end = False
    subscription.save(update_fields=["cancel_at_period_end", "updated_at"])

    record_audit(
        AuditAction.SUBSCRIPTION_CHANGED,
        user=user,
        request=request,
        obj=subscription.organization,
        description="Cancelled the pending cancellation",
        metadata={"plan": subscription.plan.slug},
    )
    return subscription
