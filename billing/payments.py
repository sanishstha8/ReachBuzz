"""
Generating invoices, collecting them, and reconciling what comes back.

Everything in this module is written around one assumption: **it will be run
twice.** Celery retries, providers redeliver webhooks for days, and operators
re-run jobs after an outage. So every operation here is idempotent, and where
idempotency depends on a check-then-act, a database constraint sits underneath
it as the backstop — because the check can be wrong and the constraint cannot.

The other rule is that **the provider reports and this module decides.** A
provider says "charge abc123 succeeded"; whether that means an invoice is now
paid is settled here, against the invoice's own state. Letting a provider mark
its own charges settled would make a redelivered webhook indistinguishable from
a second payment.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.invoicing import (
    Invoice,
    InvoiceLine,
    InvoiceSequence,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    money,
)
from billing.models import Subscription
from billing.providers.base import PaymentEvent
from billing.providers.factory import get_provider, provider_name
from core.audit import record_audit
from core.models import AuditAction

logger = logging.getLogger(__name__)

#: How long a customer has to pay before an invoice is overdue.
DEFAULT_DUE_DAYS = 14


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------


def next_number(now=None) -> str:
    """
    The next invoice number, gapless, in the form ``INV-2026-000123``.

    Takes a row lock rather than reading ``max(number) + 1``, which races: two
    workers closing periods in the same instant read the same maximum and one
    loses to the unique index. Must be called inside a transaction — the lock
    is worthless outside one.
    """
    now = now or timezone.now()
    sequence, _ = InvoiceSequence.objects.select_for_update().get_or_create(year=now.year)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return f"INV-{now.year}-{sequence.last_number:06d}"


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------


@transaction.atomic
def generate_invoice(subscription: Subscription, snapshot=None) -> Invoice | None:
    """
    Draft an invoice for the period the snapshot covers.

    Returns ``None``, deliberately, for a plan with no price. Those customers
    are quoted individually — "Pricing on request" is what the page says — and
    the alternative is worse than nothing: an invoice reading 0.00 tells them
    they owe nothing this month, which is a claim this system is not in a
    position to make. It is logged so the gap is visible rather than silent.

    Idempotent through a unique constraint on (organization, period_start). A
    retried billing job gets the invoice it already made, not a second one.
    """
    plan = subscription.plan
    organization = subscription.organization

    if plan.price is None:
        logger.info(
            "No invoice for organization %s: plan %s has no price set",
            organization.pk,
            plan.slug,
        )
        return None

    period_start = snapshot.period_start if snapshot else subscription.current_period_start
    period_end = snapshot.period_end if snapshot else subscription.current_period_end

    existing = Invoice.objects.filter(
        organization=organization, period_start=period_start
    ).first()
    if existing is not None:
        return existing

    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                organization=organization,
                subscription=subscription,
                plan=plan,
                number=next_number(),
                status=InvoiceStatus.DRAFT,
                currency=plan.currency,
                period_start=period_start,
                period_end=period_end,
            )
    except IntegrityError:
        # Another worker got there between the check and the insert. Its
        # invoice is as good as the one this call would have made.
        logger.info("Invoice for organization %s already exists; reusing it", organization.pk)
        return Invoice.objects.get(organization=organization, period_start=period_start)

    InvoiceLine.objects.create(
        invoice=invoice,
        description=f"{plan.name} — {period_start:%d %b %Y} to {period_end:%d %b %Y}",
        quantity=Decimal("1.00"),
        unit_amount=money(plan.price),
    )

    invoice.recalculate()
    invoice.save(update_fields=["subtotal", "total", "updated_at"])
    return invoice


@transaction.atomic
def issue(invoice: Invoice, *, due_days: int = DEFAULT_DUE_DAYS, user=None, request=None) -> Invoice:
    """
    Move a draft to open, which freezes its numbers.

    After this the totals may not change — see ``Invoice.recalculate``. Issuing
    an already-issued invoice is a no-op rather than an error, so a retried job
    does not fail on work it already did.
    """
    if invoice.is_issued:
        return invoice

    now = timezone.now()
    invoice.status = InvoiceStatus.OPEN
    invoice.issued_at = now
    invoice.due_at = now + timezone.timedelta(days=due_days)
    invoice.save(update_fields=["status", "issued_at", "due_at", "updated_at"])

    record_audit(
        AuditAction.INVOICE_ISSUED,
        user=user,
        request=request,
        obj=invoice.organization,
        description=f"Issued invoice {invoice.number} for {invoice.total} {invoice.currency}",
        metadata={"invoice": invoice.number, "total": str(invoice.total)},
    )
    return invoice


# ---------------------------------------------------------------------------
# Collecting
# ---------------------------------------------------------------------------


def idempotency_key_for(invoice: Invoice, attempt: int) -> str:
    """
    Stable per (invoice, attempt), so a retry of the *same* attempt cannot
    double-charge while a deliberate second attempt still can be made.

    Keyed on the invoice number rather than its primary key because the number
    is what appears on the provider's dashboard, which is where somebody
    reconciling by hand will be looking.
    """
    return f"{invoice.number}:{attempt}"


@transaction.atomic
def collect(invoice: Invoice, *, user=None, request=None) -> Payment | None:
    """
    Ask the provider for the money.

    Returns ``None`` when there is nothing to do — already settled, or not yet
    issued. Neither is an error: a collection sweep runs over everything open
    and should be boring.
    """
    if invoice.status != InvoiceStatus.OPEN:
        return None
    if invoice.is_settled:
        _mark_paid(invoice)
        return None

    provider = get_provider()
    provider.check_configuration()

    attempt = invoice.payments.count() + 1
    key = idempotency_key_for(invoice, attempt)
    amount = invoice.amount_due

    payment = Payment.objects.create(
        invoice=invoice,
        provider=provider_name(),
        idempotency_key=key,
        amount=amount,
        currency=invoice.currency,
        status=PaymentStatus.PENDING,
    )

    result = provider.charge(
        amount=amount,
        currency=invoice.currency,
        idempotency_key=key,
        description=f"{invoice.plan.name} ({invoice.number})",
        metadata={"invoice": invoice.number, "organization": str(invoice.organization_id)},
    )

    if result.success:
        payment.status = PaymentStatus.SUCCEEDED
        payment.provider_reference = result.provider_reference
        payment.succeeded_at = timezone.now()
        payment.raw = result.raw
        payment.save(
            update_fields=["status", "provider_reference", "succeeded_at", "raw", "updated_at"]
        )
        apply_payment(invoice, amount, user=user, request=request)
    else:
        payment.status = PaymentStatus.FAILED
        payment.error_code = result.error_code
        payment.error_message = result.error_message[:255]
        payment.raw = result.raw
        payment.save(
            update_fields=["status", "error_code", "error_message", "raw", "updated_at"]
        )
        logger.warning(
            "Payment failed for invoice %s: %s (%s)",
            invoice.number,
            result.error_code,
            "retryable" if result.retryable else "permanent",
        )

    return payment


@transaction.atomic
def apply_payment(invoice: Invoice, amount: Decimal, *, user=None, request=None) -> Invoice:
    """
    Credit an amount against an invoice and settle it if that clears the total.

    Locked with ``select_for_update``. Two webhooks for the same invoice
    arriving together would otherwise both read the old ``amount_paid`` and
    both write their own, losing one of the payments.
    """
    locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
    locked.amount_paid = money(locked.amount_paid + money(amount))
    locked.save(update_fields=["amount_paid", "updated_at"])

    if locked.is_settled and locked.status == InvoiceStatus.OPEN:
        _mark_paid(locked, user=user, request=request)

    invoice.refresh_from_db()
    return invoice


def _mark_paid(invoice: Invoice, *, user=None, request=None) -> None:
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = timezone.now()
    invoice.save(update_fields=["status", "paid_at", "updated_at"])

    record_audit(
        AuditAction.INVOICE_PAID,
        user=user,
        request=request,
        obj=invoice.organization,
        description=f"Invoice {invoice.number} paid",
        metadata={"invoice": invoice.number, "total": str(invoice.total)},
    )

    # A payment clears a past-due subscription. Done here rather than left to
    # the customer to notice, because the whole point of past-due is that it is
    # temporary.
    subscription = invoice.subscription
    if subscription is not None:
        from billing.models import SubscriptionStatus

        if subscription.status == SubscriptionStatus.PAST_DUE:
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.save(update_fields=["status", "updated_at"])


@transaction.atomic
def void(invoice: Invoice, *, reason: str = "", user=None, request=None) -> Invoice:
    """
    Cancel an invoice that should not have been issued.

    Voiding rather than deleting. The number stays taken, because a gap in the
    sequence is exactly what a gapless sequence exists to avoid, and because
    "invoice 41 was cancelled" is a better answer than "invoice 41 never
    existed" to anyone holding a copy of it.
    """
    if invoice.status == InvoiceStatus.PAID:
        raise ValueError(f"Invoice {invoice.number} is paid; refund it rather than voiding it.")

    invoice.status = InvoiceStatus.VOID
    invoice.voided_at = timezone.now()
    invoice.note = reason[:255]
    invoice.save(update_fields=["status", "voided_at", "note", "updated_at"])

    record_audit(
        AuditAction.INVOICE_VOIDED,
        user=user,
        request=request,
        obj=invoice.organization,
        description=f"Voided invoice {invoice.number}",
        metadata={"invoice": invoice.number, "reason": reason},
    )
    return invoice


# ---------------------------------------------------------------------------
# Reconciling what the provider tells us
# ---------------------------------------------------------------------------


@transaction.atomic
def apply_event(event: PaymentEvent) -> bool:
    """
    Act on one parsed provider event. Returns whether anything changed.

    Matched to a payment by ``provider_reference``. An event about a charge
    this system has never heard of is logged and ignored rather than
    guessed at — inventing an invoice to attach it to would be worse than
    leaving a human to reconcile it.
    """
    payment = (
        Payment.objects.select_for_update()
        .select_related("invoice")
        .filter(provider_reference=event.reference)
        .exclude(provider_reference="")
        .first()
    )
    if payment is None:
        logger.warning("Payment event %s references unknown charge %s", event.event_id, event.reference)
        return False

    if event.event_type == PaymentEvent.SUCCEEDED:
        if payment.status == PaymentStatus.SUCCEEDED:
            return False  # Already credited. A redelivery, not a second payment.
        payment.status = PaymentStatus.SUCCEEDED
        payment.succeeded_at = event.occurred_at or timezone.now()
        payment.save(update_fields=["status", "succeeded_at", "updated_at"])
        apply_payment(payment.invoice, event.amount or payment.amount)
        return True

    if event.event_type == PaymentEvent.FAILED:
        if payment.status == PaymentStatus.FAILED:
            return False
        payment.status = PaymentStatus.FAILED
        payment.save(update_fields=["status", "updated_at"])
        _mark_subscription_past_due(payment.invoice)
        return True

    if event.event_type == PaymentEvent.REFUNDED:
        if payment.status == PaymentStatus.REFUNDED:
            return False
        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=["status", "updated_at"])

        invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id)
        invoice.amount_paid = money(max(Decimal("0.00"), invoice.amount_paid - payment.amount))
        invoice.status = InvoiceStatus.OPEN if not invoice.is_settled else invoice.status
        invoice.paid_at = None if not invoice.is_settled else invoice.paid_at
        invoice.save(update_fields=["amount_paid", "status", "paid_at", "updated_at"])
        return True

    return False


def _mark_subscription_past_due(invoice: Invoice) -> None:
    """
    A failed payment makes a subscription past-due, not cancelled.

    Past-due still sends. Cutting a business off the moment a card expires is
    how a customer discovers a billing problem from their own customers.
    """
    from billing.models import ENTITLED_STATUSES, SubscriptionStatus

    subscription = invoice.subscription
    if subscription is None or subscription.status not in ENTITLED_STATUSES:
        return
    if subscription.status == SubscriptionStatus.PAST_DUE:
        return

    subscription.status = SubscriptionStatus.PAST_DUE
    subscription.save(update_fields=["status", "updated_at"])
    logger.info("Subscription for organization %s is past due", subscription.organization_id)
