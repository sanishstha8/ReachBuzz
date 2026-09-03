"""
Invoicing, collection and reconciliation.

Almost every test here is about something happening **twice** — a retried job, a
redelivered webhook, a double-clicked button — because that is the failure mode
that matters when money is involved and the one that never shows up in a happy
path. The second occurrence must change nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from billing import payments
from billing.invoicing import (
    Invoice,
    InvoiceSequence,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    money,
)
from billing.models import Subscription, SubscriptionStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def priced_plan(make_plan):
    """A plan somebody can actually be billed for."""
    return make_plan("priced", price=Decimal("29.00"), currency="USD")


@pytest.fixture
def billable(organization, on_plan, priced_plan):
    return on_plan(organization, priced_plan)


class TestMoney:
    def test_it_is_decimal_all_the_way_down(self) -> None:
        """0.1 + 0.2 is a rounding curiosity elsewhere and a discrepancy here."""
        assert money(0.1) + money(0.2) == Decimal("0.30")

    def test_half_a_cent_rounds_up_not_to_even(self) -> None:
        """
        Banker's rounding is defensible statistically and indefensible on an
        invoice somebody is reading.
        """
        assert money("0.125") == Decimal("0.13")
        assert money("0.135") == Decimal("0.14")


class TestNumbering:
    def test_numbers_are_sequential_and_gapless(self, organization) -> None:
        from django.db import transaction

        with transaction.atomic():
            first = payments.next_number()
            second = payments.next_number()

        year = timezone.now().year
        assert first == f"INV-{year}-000001"
        assert second == f"INV-{year}-000002"

    def test_the_counter_is_per_year(self, organization) -> None:
        from datetime import datetime

        from django.db import transaction

        with transaction.atomic():
            payments.next_number(datetime(2026, 6, 1, tzinfo=timezone.get_current_timezone()))
            number = payments.next_number(
                datetime(2027, 1, 1, tzinfo=timezone.get_current_timezone())
            )

        assert number == "INV-2027-000001"
        assert InvoiceSequence.objects.count() == 2


class TestGenerating:
    def test_it_bills_the_plan_price(self, billable) -> None:
        invoice = payments.generate_invoice(billable)

        assert invoice.total == Decimal("29.00")
        assert invoice.lines.count() == 1
        assert invoice.status == InvoiceStatus.DRAFT

    def test_an_unpriced_plan_gets_no_invoice(self, organization, on_plan, plans) -> None:
        """
        Not a zero invoice. "Pricing on request" means the figure is agreed
        individually, and a 0.00 invoice would tell the customer they owe
        nothing this month — a claim this system is in no position to make.
        """
        subscription = on_plan(organization, plans["starter"])

        assert payments.generate_invoice(subscription) is None
        assert Invoice.objects.count() == 0

    def test_generating_twice_produces_one_invoice(self, billable) -> None:
        """A retried billing job must not bill the month again."""
        first = payments.generate_invoice(billable)
        second = payments.generate_invoice(billable)

        assert first.pk == second.pk
        assert Invoice.objects.count() == 1

    def test_the_line_records_what_was_charged_not_todays_price(
        self, billable, priced_plan
    ) -> None:
        """A price change next month must not rewrite last month's invoice."""
        invoice = payments.generate_invoice(billable)

        priced_plan.price = Decimal("99.00")
        priced_plan.save()
        invoice.refresh_from_db()

        assert invoice.total == Decimal("29.00")


class TestIssuing:
    def test_issuing_freezes_the_numbers(self, billable) -> None:
        invoice = payments.issue(payments.generate_invoice(billable))

        assert invoice.status == InvoiceStatus.OPEN
        assert invoice.due_at is not None
        with pytest.raises(ValueError, match="cannot be recalculated"):
            invoice.recalculate()

    def test_issuing_twice_is_a_no_op(self, billable) -> None:
        invoice = payments.generate_invoice(billable)
        payments.issue(invoice)
        issued_at = invoice.issued_at

        payments.issue(invoice)

        assert invoice.issued_at == issued_at

    def test_it_is_audited(self, billable) -> None:
        from core.models import AuditAction, AuditLog

        payments.issue(payments.generate_invoice(billable))

        assert AuditLog.objects.filter(action=AuditAction.INVOICE_ISSUED).exists()


class TestCollecting:
    def test_a_successful_charge_settles_the_invoice(self, billable) -> None:
        invoice = payments.issue(payments.generate_invoice(billable))

        payment = payments.collect(invoice)
        invoice.refresh_from_db()

        assert payment.status == PaymentStatus.SUCCEEDED
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.amount_due == Decimal("0.00")

    def test_a_declined_charge_leaves_the_invoice_open(self, billable, settings) -> None:
        settings.MOCK_PAYMENT_FAILURE_RATE = 1.0
        invoice = payments.issue(payments.generate_invoice(billable))

        payment = payments.collect(invoice)
        invoice.refresh_from_db()

        assert payment.status == PaymentStatus.FAILED
        assert invoice.status == InvoiceStatus.OPEN
        assert invoice.amount_due == Decimal("29.00")

    def test_every_attempt_is_kept(self, billable, settings) -> None:
        """
        "We tried twice" is a fact a customer may need explained and a merchant
        may need to prove, so attempts accumulate rather than overwrite.
        """
        settings.MOCK_PAYMENT_FAILURE_RATE = 1.0
        invoice = payments.issue(payments.generate_invoice(billable))

        payments.collect(invoice)
        payments.collect(invoice)

        assert invoice.payments.count() == 2

    def test_each_attempt_gets_its_own_idempotency_key(self, billable, settings) -> None:
        """
        A retry of the same attempt must not double-charge; a deliberate second
        attempt must still be possible. Both need the key to be per-attempt.
        """
        settings.MOCK_PAYMENT_FAILURE_RATE = 1.0
        invoice = payments.issue(payments.generate_invoice(billable))

        payments.collect(invoice)
        payments.collect(invoice)

        keys = set(invoice.payments.values_list("idempotency_key", flat=True))
        assert keys == {f"{invoice.number}:1", f"{invoice.number}:2"}

    def test_collecting_a_settled_invoice_does_nothing(self, billable) -> None:
        invoice = payments.issue(payments.generate_invoice(billable))
        payments.collect(invoice)
        invoice.refresh_from_db()

        assert payments.collect(invoice) is None
        assert invoice.payments.count() == 1

    def test_a_draft_is_not_collected(self, billable) -> None:
        """Nobody has been told they owe it yet."""
        assert payments.collect(payments.generate_invoice(billable)) is None

    def test_paying_clears_a_past_due_subscription(self, billable) -> None:
        """Past-due is meant to be temporary; nobody should have to notice."""
        Subscription.objects.filter(pk=billable.pk).update(status=SubscriptionStatus.PAST_DUE)
        invoice = payments.issue(payments.generate_invoice(billable))

        payments.collect(invoice)

        billable.refresh_from_db()
        assert billable.status == SubscriptionStatus.ACTIVE


class TestTheProviderHonoursIdempotency:
    def test_the_same_key_returns_the_first_result(self) -> None:
        """
        A mock that charged twice for one key would let a double-charge bug
        pass every test here and appear in production.
        """
        from billing.providers.mock_provider import MockPaymentProvider

        provider = MockPaymentProvider()
        first = provider.charge(amount=Decimal("10.00"), currency="USD", idempotency_key="k")
        second = provider.charge(amount=Decimal("10.00"), currency="USD", idempotency_key="k")

        assert first.provider_reference == second.provider_reference

    def test_charging_without_a_key_is_refused(self) -> None:
        from billing.providers.mock_provider import MockPaymentProvider

        with pytest.raises(ValueError, match="idempotency key"):
            MockPaymentProvider().charge(
                amount=Decimal("1.00"), currency="USD", idempotency_key=""
            )


class TestVoiding:
    def test_a_voided_invoice_keeps_its_number(self, billable) -> None:
        """A gap in the sequence is exactly what a gapless sequence avoids."""
        invoice = payments.issue(payments.generate_invoice(billable))
        number = invoice.number

        payments.void(invoice, reason="Issued in error")

        assert invoice.status == InvoiceStatus.VOID
        assert Invoice.objects.get(pk=invoice.pk).number == number

    def test_a_paid_invoice_cannot_be_voided(self, billable) -> None:
        invoice = payments.issue(payments.generate_invoice(billable))
        payments.collect(invoice)
        invoice.refresh_from_db()

        with pytest.raises(ValueError, match="refund it"):
            payments.void(invoice)


class TestApplyingProviderEvents:
    @pytest.fixture
    def pending(self, billable, settings):
        """An invoice with a charge the provider has not confirmed yet."""
        invoice = payments.issue(payments.generate_invoice(billable))
        payment = Payment.objects.create(
            invoice=invoice,
            provider="mock",
            provider_reference="ref_123",
            idempotency_key=f"{invoice.number}:1",
            amount=invoice.total,
            currency=invoice.currency,
            status=PaymentStatus.PENDING,
        )
        return invoice, payment

    def _event(self, event_type, reference="ref_123", amount=None, event_id="evt_1"):
        from billing.providers.base import PaymentEvent

        return PaymentEvent(
            event_id=event_id,
            event_type=event_type,
            reference=reference,
            amount=amount,
        )

    def test_a_success_event_settles_the_invoice(self, pending) -> None:
        from billing.providers.base import PaymentEvent

        invoice, _ = pending

        assert payments.apply_event(self._event(PaymentEvent.SUCCEEDED)) is True

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID

    def test_the_same_event_twice_credits_once(self, pending) -> None:
        """The redelivery case. A provider retries for days."""
        from billing.providers.base import PaymentEvent

        invoice, _ = pending
        payments.apply_event(self._event(PaymentEvent.SUCCEEDED))

        assert payments.apply_event(self._event(PaymentEvent.SUCCEEDED)) is False

        invoice.refresh_from_db()
        assert invoice.amount_paid == Decimal("29.00")

    def test_a_failure_event_makes_the_subscription_past_due(self, pending, billable) -> None:
        from billing.providers.base import PaymentEvent

        payments.apply_event(self._event(PaymentEvent.FAILED))

        billable.refresh_from_db()
        assert billable.status == SubscriptionStatus.PAST_DUE

    def test_past_due_still_sends(self, pending, billable, organization) -> None:
        """A failed card starts a conversation; it does not sever messaging."""
        from billing import usage
        from billing.providers.base import PaymentEvent

        payments.apply_event(self._event(PaymentEvent.FAILED))

        assert usage.is_entitled(organization) is True

    def test_a_refund_reopens_the_invoice(self, pending) -> None:
        from billing.providers.base import PaymentEvent

        invoice, _ = pending
        payments.apply_event(self._event(PaymentEvent.SUCCEEDED))

        payments.apply_event(self._event(PaymentEvent.REFUNDED))

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.OPEN
        assert invoice.amount_paid == Decimal("0.00")
        assert invoice.paid_at is None

    def test_an_event_for_an_unknown_charge_is_ignored(self, pending) -> None:
        """Inventing an invoice to attach it to would be worse than leaving it."""
        from billing.providers.base import PaymentEvent

        assert payments.apply_event(self._event(PaymentEvent.SUCCEEDED, reference="nope")) is False


class TestInvoicingOnPeriodClose:
    def test_closing_a_period_issues_an_invoice(self, billable) -> None:
        from billing import usage

        usage.close_period(billable)

        invoice = Invoice.objects.get()
        assert invoice.status == InvoiceStatus.OPEN
        assert invoice.total == Decimal("29.00")

    def test_an_unpriced_plan_closes_without_one(self, organization, on_plan, plans) -> None:
        from billing import usage

        usage.close_period(on_plan(organization, plans["starter"]))

        assert Invoice.objects.count() == 0

    def test_the_period_still_rolls_when_invoicing_fails(
        self, billable, monkeypatch
    ) -> None:
        """
        One bad invoice must not freeze a customer's message quota — the quota
        resets with the period, so a period that cannot roll is a customer who
        cannot send, punished for our fault.
        """
        from billing import usage

        def explode(*args, **kwargs):
            raise RuntimeError("invoicing is broken")

        monkeypatch.setattr(payments, "generate_invoice", explode)
        started_at = billable.current_period_start

        usage.close_period(billable)

        billable.refresh_from_db()
        assert billable.current_period_start > started_at
