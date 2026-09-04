"""
Invoices, payments, and the record of what a provider told us.

Split from :mod:`billing.models` because that file is about *entitlement* — what
a customer may do — and this one is about *money*, which has different rules.
Chief among them:

**An issued invoice is immutable.** It is not edited, it is voided and reissued.
An invoice is a statement of what was owed at a moment; a system that can
rewrite one cannot be reconciled against anything, and a customer holding a PDF
that no longer matches the database has every reason to distrust both.

**Money is Decimal.** Never float, never a Python ``round()`` on a binary
fraction. Every amount in this module is ``Decimal`` with two places, quantized
explicitly at the point a total is computed.

**Nothing here stores a card.** Not a number, not a last four, not an expiry.
The provider holds the instrument and we hold a reference to it — the same rule
the rest of this project applies to Meta credentials.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from billing.models import Plan, Subscription
from core.models import BaseModel
from organizations.models import Organization

#: Every monetary value in this module is quantized to this before it is stored.
CENTS = Decimal("0.01")


def money(value) -> Decimal:
    """
    Coerce to a two-place Decimal, rounding the way an accountant expects.

    ``ROUND_HALF_UP`` rather than Python's default banker's rounding: half a
    cent going to the even number is defensible statistically and indefensible
    on an invoice a customer is reading.
    """
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


class InvoiceSequence(models.Model):
    """
    The counter behind invoice numbers.

    A row per year, taken under ``select_for_update``. The obvious alternative
    — ``max(number) + 1`` — races: two workers closing periods at the same
    instant read the same maximum and one of them loses to the unique index,
    which turns a routine billing run into a failed job.

    Numbers must also be **gapless**. Several tax authorities require it, and
    "invoice 41 does not exist" is a question no finance team enjoys. That rules
    out a database sequence, which does not roll back with its transaction.
    """

    year = models.PositiveSmallIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = _("invoice sequence")
        verbose_name_plural = _("invoice sequences")

    def __str__(self) -> str:
        return f"{self.year}: {self.last_number}"


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    OPEN = "open", _("Open")
    PAID = "paid", _("Paid")
    VOID = "void", _("Void")
    UNCOLLECTIBLE = "uncollectible", _("Uncollectible")


#: Statuses after which the invoice's numbers may no longer change.
ISSUED_STATUSES = frozenset(
    {InvoiceStatus.OPEN, InvoiceStatus.PAID, InvoiceStatus.VOID, InvoiceStatus.UNCOLLECTIBLE}
)


class InvoiceQuerySet(models.QuerySet):
    def for_organization(self, organization) -> InvoiceQuerySet:
        """``None`` returns nothing, matching every other scoped queryset."""
        if organization is None:
            return self.none()
        return self.filter(organization=organization)

    def outstanding(self) -> InvoiceQuerySet:
        return self.filter(status=InvoiceStatus.OPEN)

    def overdue(self, now=None) -> InvoiceQuerySet:
        return self.outstanding().filter(due_at__lt=now or timezone.now())


class Invoice(BaseModel):
    """
    What one organization owed for one period.

    Not an :class:`~organizations.scoping.OrganizationOwnedModel` by accident —
    it is one in every respect that matters (the FK, the scoped queryset, the
    ``None`` behaviour) but declares them directly, because an invoice must
    survive its organization being deleted long enough to be reconciled.
    ``PROTECT`` rather than ``CASCADE`` is the whole difference, and it is the
    difference between closing an account and destroying its financial record.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="invoices"
    )
    subscription = models.ForeignKey(
        Subscription, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="invoices")

    #: Human-facing, sequential, never reused. See billing.payments.next_number.
    number = models.CharField(_("number"), max_length=32, unique=True)

    status = models.CharField(
        max_length=16, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT, db_index=True
    )

    currency = models.CharField(max_length=3, default="USD")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    period_start = models.DateTimeField()
    period_end = models.DateTimeField()

    issued_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    #: Why it was voided or written off. Shown to nobody but an operator.
    note = models.CharField(max_length=255, blank=True)

    objects = InvoiceQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("invoice")
        verbose_name_plural = _("invoices")
        constraints = [
            # One invoice per organization per period. The guard against a
            # retried billing job billing the same month twice.
            models.UniqueConstraint(
                fields=["organization", "period_start"], name="unique_invoice_per_period"
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=Decimal("0.00")),
                name="invoice_total_not_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "due_at"], name="invoice_due_idx"),
            models.Index(fields=["organization", "-created_at"], name="invoice_org_recent_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.number} ({self.get_status_display()})"

    @property
    def is_issued(self) -> bool:
        return self.status in ISSUED_STATUSES

    @property
    def amount_due(self) -> Decimal:
        return money(self.total - self.amount_paid)

    @property
    def is_settled(self) -> bool:
        """Paid in full. Compared as Decimal, so a cent short is not settled."""
        return self.amount_due <= Decimal("0.00")

    @property
    def is_overdue(self) -> bool:
        return (
            self.status == InvoiceStatus.OPEN
            and self.due_at is not None
            and self.due_at < timezone.now()
        )

    def recalculate(self) -> None:
        """
        Re-total from the lines. Refuses once the invoice has been issued.

        The refusal is the point. Recalculating an open invoice is how a total
        drifts away from the figure a customer was actually sent.
        """
        if self.is_issued:
            raise ValueError(f"Invoice {self.number} is issued and cannot be recalculated.")

        self.subtotal = money(sum((line.amount for line in self.lines.all()), Decimal("0.00")))
        self.total = money(self.subtotal + self.tax)


class InvoiceLine(models.Model):
    """
    One charge on an invoice.

    Amounts are stored rather than derived on read. A line says what was
    charged, not what today's price list would charge — a plan whose price
    changes next month must not silently rewrite last month's invoice.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.00"))
    unit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["id"]
        verbose_name = _("invoice line")
        verbose_name_plural = _("invoice lines")

    def __str__(self) -> str:
        return f"{self.description}: {self.amount}"

    def save(self, *args, **kwargs):
        self.amount = money(Decimal(str(self.quantity)) * Decimal(str(self.unit_amount)))
        super().save(*args, **kwargs)


class PaymentStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")
    REFUNDED = "refunded", _("Refunded")


class Payment(BaseModel):
    """
    One attempt to move money against one invoice.

    Attempts are kept, not overwritten. A declined card followed by a
    successful one is two rows, because "we tried twice" is a fact a customer
    may need explained and a merchant may need to prove.

    ``provider_reference`` is unique where present. That constraint is the
    backstop against double-crediting a replayed webhook: the database refuses
    the second row even if every check above it has been got wrong.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=32)
    provider_reference = models.CharField(max_length=255, blank=True, db_index=True)

    #: What we sent to the provider so a retry cannot become a second charge.
    idempotency_key = models.CharField(max_length=255, unique=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING, db_index=True
    )

    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    #: The provider's own response, kept whole. Our reading of it is not evidence.
    raw = models.JSONField(default=dict, blank=True)

    succeeded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("payment")
        verbose_name_plural = _("payments")
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_reference"],
                condition=~models.Q(provider_reference=""),
                name="unique_payment_provider_reference",
            )
        ]

    def __str__(self) -> str:
        return f"{self.amount} {self.currency} ({self.get_status_display()})"


class PaymentWebhookStatus(models.TextChoices):
    RECEIVED = "received", _("Received")
    PROCESSED = "processed", _("Processed")
    IGNORED = "ignored", _("Ignored (nothing to do)")
    FAILED = "failed", _("Failed")
    REJECTED = "rejected", _("Rejected (bad signature)")


class PaymentWebhookEvent(BaseModel):
    """
    A raw notification from a payment provider, stored before it is interpreted.

    Same shape and the same reasoning as ``whatsapp.models.WebhookEvent``:
    persist first, answer 200, process afterwards. A provider retries a non-200
    for days, so an endpoint that does its work inline turns one slow query into
    a flood of duplicates — and with money involved, each duplicate is a chance
    to credit the same payment twice.

    ``event_id`` is unique, which is what makes a redelivery a no-op rather than
    a second credit. Note it is the id of the *notification*, not of the charge:
    one charge legitimately produces several events.
    """

    provider = models.CharField(max_length=32)
    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    signature_valid = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16,
        choices=PaymentWebhookStatus.choices,
        default=PaymentWebhookStatus.RECEIVED,
        db_index=True,
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("payment webhook event")
        verbose_name_plural = _("payment webhook events")
        indexes = [
            models.Index(fields=["status", "-created_at"], name="pay_webhook_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.event_type} ({self.get_status_display()})"
