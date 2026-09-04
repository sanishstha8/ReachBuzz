"""
What an organization has used, and whether it may use more.

**Usage is derived, never accumulated.** Every number here is a COUNT over the
rows that actually exist. The tempting alternative — a counter incremented on
each send — is faster and wrong: it drifts on a retry, on a crash between the
send and the increment, and on any bulk correction, and a billing figure that
quietly disagrees with the message log is the worst kind of wrong. The counts
are indexed and scoped to one organization and one period, so the query stays
cheap; if it ever stops being cheap, :class:`~billing.models.UsageSnapshot`
is the place to cache it, not a live counter.

**Messages are metered when sent, not when queued.** A message that never left
the building cost the customer nothing, and billing for a campaign that failed
preflight would be indefensible. The pre-launch check in
:func:`check_can_send` is the other half of that: it refuses a campaign that
*would* exceed the ceiling, so nobody is billed for a half-delivered send.
"""

from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

from billing.models import ENTITLED_STATUSES, Plan, Subscription, SubscriptionStatus
from core.exceptions import ValidationFailed

logger = logging.getLogger(__name__)


class QuotaExceeded(ValidationFailed):
    """
    A plan limit stands in the way.

    A subclass of the project's existing validation error rather than a new
    exception type, so every caller that already renders ``ValidationFailed``
    with its ``blockers`` list — the campaign wizard, the REST error handler —
    shows this correctly without being taught anything.
    """


# ---------------------------------------------------------------------------
# Resolving the plan
# ---------------------------------------------------------------------------


def subscription_for(organization) -> Subscription | None:
    if organization is None:
        return None
    return (
        Subscription.objects.select_related("plan").filter(organization=organization).first()
    )


def plan_for(organization) -> Plan | None:
    """
    The plan whose limits apply to this organization.

    A missing subscription resolves to the cheapest public plan rather than to
    either extreme, and says so loudly in the log. Treating it as unlimited
    would give away the product to anyone whose signup half-failed; treating it
    as blocked would take a working customer offline over a data problem they
    did not cause. The fallback is a bug either way — hence the warning — but it
    is a bug that leaves them able to work and us able to notice.
    """
    subscription = subscription_for(organization)
    if subscription is not None:
        return subscription.plan

    fallback = Plan.objects.public().first()
    logger.warning(
        "Organization %s has no subscription; falling back to plan %s",
        getattr(organization, "pk", None),
        getattr(fallback, "slug", None),
    )
    return fallback


def is_entitled(organization) -> bool:
    """Whether this organization's subscription still permits using the product."""
    subscription = subscription_for(organization)
    if subscription is None:
        return True  # See plan_for: a missing row must not lock a customer out.
    return subscription.status in ENTITLED_STATUSES


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def current_period(organization) -> tuple[object, object]:
    """
    The window usage is measured over.

    Falls back to the current calendar month when there is no subscription, so
    the number shown on a dashboard is never blank and never the all-time total.
    """
    subscription = subscription_for(organization)
    if subscription is not None:
        return subscription.current_period_start, subscription.current_period_end

    now = timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    from billing.models import add_months

    return start, add_months(start, 1)


def messages_sent(organization, since=None, until=None) -> int:
    """
    Messages this organization actually put on the wire in the period.

    Imported here rather than at module scope: ``messaging`` imports campaign
    services, which will import this module for the launch check, and a
    top-level import would close that loop.
    """
    from messaging.models import Message

    if organization is None:
        return 0
    if since is None or until is None:
        since, until = current_period(organization)

    return (
        Message.objects.for_organization(organization)
        .filter(sent_at__isnull=False, sent_at__gte=since, sent_at__lt=until)
        .count()
    )


def contacts_held(organization) -> int:
    from contacts.models import Contact

    if organization is None:
        return 0
    return Contact.objects.for_organization(organization).count()


def team_members(organization) -> int:
    from organizations.models import OrganizationMember

    if organization is None:
        return 0
    return OrganizationMember.objects.filter(organization=organization).count()


#: Metric name -> how to count what is already used.
COUNTERS = {
    "max_messages_per_month": messages_sent,
    "max_contacts": contacts_held,
    "max_team_members": team_members,
}


def summary(organization) -> dict:
    """
    Everything a billing page needs, in one call.

    ``limit`` of ``None`` means unlimited and ``percent`` is then ``None`` too —
    a progress bar against no ceiling is meaningless, and rendering 0% would
    imply one exists.
    """
    plan = plan_for(organization)
    start, end = current_period(organization)
    subscription = subscription_for(organization)

    metrics = {}
    for metric, counter in COUNTERS.items():
        used = counter(organization)
        ceiling = plan.limit(metric) if plan else None
        exceeded = bool(ceiling is not None and used > ceiling)
        metrics[metric] = {
            "used": used,
            "limit": ceiling,
            "remaining": plan.remaining(metric, used) if plan else None,
            "percent": None if not ceiling else min(100, round(used / ceiling * 100)),
            "exceeded": exceeded,
            # Computed here rather than in a template: Django's `add` filter
            # cannot subtract, and the arithmetic that looks like it does is
            # addition wearing a disguise.
            "over_by": (used - ceiling) if exceeded else 0,
        }

    return {
        "plan": plan,
        "subscription": subscription,
        "period_start": start,
        "period_end": end,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def check(organization, metric: str, additional: int = 1) -> None:
    """
    Raise :class:`QuotaExceeded` if ``additional`` more would not fit.

    Called before the work, with the size of the work, so the refusal happens
    while nothing has been half-done.
    """
    plan = plan_for(organization)
    if plan is None:  # No catalogue at all — an install with no plans seeded.
        return

    used = COUNTERS[metric](organization)
    if plan.allows(metric, used, additional):
        return

    ceiling = plan.limit(metric)
    raise QuotaExceeded(
        _MESSAGES[metric]["headline"].format(plan=plan.name),
        details={
            "blockers": [
                _MESSAGES[metric]["detail"].format(
                    used=used, limit=ceiling, plan=plan.name, additional=additional
                )
            ],
            "metric": metric,
            "used": used,
            "limit": ceiling,
        },
    )


def check_can_send(organization, recipients: int) -> None:
    """
    The gate in front of a campaign launch.

    Two separate refusals, because they need different remedies: a subscription
    that has lapsed needs paying, and a quota that is spent needs either waiting
    or upgrading. Collapsing them into one message would send half the customers
    to the wrong page.
    """
    subscription = subscription_for(organization)
    if subscription is not None and subscription.status not in ENTITLED_STATUSES:
        raise QuotaExceeded(
            "This subscription is not active.",
            details={
                "blockers": [
                    f"The subscription for {organization.name} is "
                    f"{subscription.get_status_display().lower()}. "
                    "Reactivate it to send campaigns."
                ],
                "status": subscription.status,
            },
        )

    check(organization, "max_messages_per_month", additional=recipients)


_MESSAGES = {
    "max_messages_per_month": {
        "headline": "This send would go past the {plan} monthly message limit.",
        "detail": (
            "You have sent {used} of {limit} messages this period, and this campaign "
            "needs {additional} more. Upgrade the plan, or wait for the period to reset."
        ),
    },
    "max_contacts": {
        "headline": "The {plan} contact limit is full.",
        "detail": (
            "You are holding {used} of {limit} contacts. Upgrade the plan, or remove "
            "contacts you no longer message."
        ),
    },
    "max_team_members": {
        "headline": "The {plan} team limit is full.",
        "detail": "Your organization has {used} of {limit} members. Upgrade to add more.",
    },
}


# ---------------------------------------------------------------------------
# Closing a period
# ---------------------------------------------------------------------------


def close_period(subscription: Subscription):
    """
    Freeze the finished period's totals and roll the subscription forward.

    Idempotent: the unique constraint on (organization, period_start) means a
    retry cannot double-count a period, and re-running after a partial failure
    picks up where it stopped. Billing jobs get retried, so this has to be safe
    to run twice.
    """
    from billing.models import UsageSnapshot

    snapshot, created = UsageSnapshot.objects.get_or_create(
        organization=subscription.organization,
        period_start=subscription.current_period_start,
        defaults={
            "plan": subscription.plan,
            "period_end": subscription.current_period_end,
            "messages_sent": messages_sent(
                subscription.organization,
                subscription.current_period_start,
                subscription.current_period_end,
            ),
            "contacts_at_close": contacts_held(subscription.organization),
        },
    )

    # Bill the period that just closed, before moving on from it. Generation is
    # idempotent on (organization, period_start), so a retry that gets this far
    # twice produces one invoice.
    _invoice_closed_period(subscription, snapshot)

    if subscription.cancel_at_period_end:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.canceled_at = timezone.now()
        subscription.save(update_fields=["status", "canceled_at", "updated_at"])
        return snapshot, created

    subscription.current_period_start = subscription.current_period_end
    subscription.current_period_end = subscription.next_period_end(
        subscription.current_period_start
    )
    # A trial that reaches its end becomes an ordinary subscription; Stage 4
    # decides whether it was paid for.
    if subscription.status == SubscriptionStatus.TRIALING:
        subscription.status = SubscriptionStatus.ACTIVE
    subscription.save(
        update_fields=["current_period_start", "current_period_end", "status", "updated_at"]
    )
    return snapshot, created


def _invoice_closed_period(subscription: Subscription, snapshot) -> None:
    """
    Draft and issue an invoice for the period being closed.

    Imported inside the function: ``billing.payments`` imports this module for
    nothing today, but it imports ``billing.invoicing``, and keeping the
    direction of the dependency obvious at the call site is cheaper than
    working out later why an import loop appeared.

    Failures are logged, not raised. A billing period must still roll forward
    when invoicing has a problem — otherwise one bad invoice freezes a
    customer's message quota, which punishes them for our fault.
    """
    from billing import payments

    try:
        invoice = payments.generate_invoice(subscription, snapshot)
        if invoice is not None:
            payments.issue(invoice)
    except Exception:
        logger.exception(
            "Could not invoice organization %s for the period ending %s",
            subscription.organization_id,
            subscription.current_period_end,
        )


def due_for_renewal(now=None):
    """Subscriptions whose period has run out."""
    now = now or timezone.now()
    return (
        Subscription.objects.select_related("organization", "plan")
        .filter(current_period_end__lte=now)
        .filter(~Q(status=SubscriptionStatus.CANCELED))
    )
