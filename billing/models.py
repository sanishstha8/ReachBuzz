"""
Plans, subscriptions, and what a customer is allowed to do.

**A limit of ``None`` means unlimited, and ``0`` means none at all.** They are
different answers and the difference matters: a nullable integer where null is
"no ceiling" is the only spelling that lets a self-hosted plan and a suspended
one both be expressed without a sentinel value like ``-1`` that every call site
has to remember.

**Plans live in the database, not in the landing page's source.** The marketing
page already advertised "Up to 1,000 contacts" as a hard-coded string while
nothing enforced it. Two sources of truth for the same promise is how a customer
ends up reading one number and hitting another, so the page now renders the same
rows this module enforces against.

**Nothing here talks to a payment provider.** A subscription records what a
customer is entitled to and until when; who took the money is Stage 4's problem,
and keeping that seam clean is what lets the provider be swapped without
touching entitlement logic.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from organizations.models import Organization

#: A limit that is not a limit. Spelled out so call sites read as prose.
UNLIMITED = None


def add_months(moment: datetime, months: int) -> datetime:
    """
    Advance a datetime by whole months, clamping to the end of short ones.

    31 January plus one month is 28 February, not 3 March. Done here rather than
    with ``dateutil.relativedelta`` because dateutil reaches this project only as
    a transitive dependency of Celery, and a billing period is not something to
    hang on a package nothing declares.
    """
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


class PlanInterval(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")
    YEARLY = "yearly", _("Yearly")


class PlanQuerySet(models.QuerySet):
    def active(self) -> PlanQuerySet:
        """Plans a customer may be subscribed to."""
        return self.filter(is_active=True)

    def public(self) -> PlanQuerySet:
        """Plans the landing page should advertise."""
        return self.active().filter(is_public=True).order_by("sort_order", "price")


class Plan(models.Model):
    """
    What a tier costs and what it permits.

    Plans are platform-level rather than customer-owned — deliberately *not*
    an :class:`~organizations.scoping.OrganizationOwnedModel`. Every customer
    sees the same catalogue, and a per-organization plan row would mean a
    price change had to be written to thousands of records.
    """

    #: Every limit is checked through :meth:`allows`, so this is the whole list.
    LIMIT_FIELDS = ("max_contacts", "max_messages_per_month", "max_team_members")

    name = models.CharField(_("name"), max_length=100, unique=True)
    slug = models.SlugField(_("slug"), max_length=120, unique=True, blank=True)
    summary = models.CharField(_("summary"), max_length=255, blank=True)

    price = models.DecimalField(
        _("price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Leave empty for 'pricing on request'. Do not invent a placeholder."),
    )
    currency = models.CharField(_("currency"), max_length=3, default="USD")
    interval = models.CharField(
        _("interval"), max_length=16, choices=PlanInterval.choices, default=PlanInterval.MONTHLY
    )

    # --- Entitlements. Empty means unlimited; zero means none. -------------
    max_contacts = models.PositiveIntegerField(
        _("contact limit"), null=True, blank=True, help_text=_("Empty means unlimited.")
    )
    max_messages_per_month = models.PositiveIntegerField(
        _("messages per month"), null=True, blank=True, help_text=_("Empty means unlimited.")
    )
    max_team_members = models.PositiveIntegerField(
        _("team members"), null=True, blank=True, help_text=_("Empty means unlimited.")
    )

    trial_days = models.PositiveSmallIntegerField(_("trial days"), default=0)

    is_active = models.BooleanField(_("active"), default=True)
    is_public = models.BooleanField(
        _("public"), default=True, help_text=_("Show this plan on the pricing page.")
    )
    featured = models.BooleanField(_("featured"), default=False)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=0)

    #: What the pricing page lists under the tier, one bullet per line.
    features = models.JSONField(_("features"), default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlanQuerySet.as_manager()

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("plan")
        verbose_name_plural = _("plans")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:120]
        super().save(*args, **kwargs)

    # -- Entitlements -------------------------------------------------------

    def limit(self, metric: str) -> int | None:
        if metric not in self.LIMIT_FIELDS:
            raise ValueError(f"{metric!r} is not a plan limit; expected one of {self.LIMIT_FIELDS}")
        return getattr(self, metric)

    def allows(self, metric: str, current: int, additional: int = 1) -> bool:
        """
        Whether ``additional`` more of ``metric`` fits inside the plan.

        Asked *before* the work, with the size of the work included, so that a
        campaign to 900 recipients against 200 remaining is refused whole rather
        than sent in part. A half-sent campaign is worse than a refused one:
        the customer pays for the half and cannot tell which half.
        """
        ceiling = self.limit(metric)
        if ceiling is None:
            return True
        return current + additional <= ceiling

    def remaining(self, metric: str, current: int) -> int | None:
        ceiling = self.limit(metric)
        if ceiling is None:
            return None
        return max(0, ceiling - current)

    @property
    def has_price(self) -> bool:
        return self.price is not None

    @property
    def is_unlimited(self) -> bool:
        return all(self.limit(field) is None for field in self.LIMIT_FIELDS)

    # -- What the pricing page renders --------------------------------------
    # These exist so the public page can loop over Plan rows directly. It used
    # to loop over a hard-coded ``Tier`` dataclass carrying the same fields;
    # matching that shape is what let the markup stay untouched.

    @property
    def price_display(self) -> str:
        """Never invents a figure - an unpriced plan says so instead."""
        if not self.has_price:
            return ""
        return f"{self.currency} {self.price:,.2f}"

    @property
    def period(self) -> str:
        if not self.has_price:
            return ""
        return "per year" if self.interval == PlanInterval.YEARLY else "per month"


class SubscriptionStatus(models.TextChoices):
    TRIALING = "trialing", _("Trialing")
    ACTIVE = "active", _("Active")
    PAST_DUE = "past_due", _("Past due")
    CANCELED = "canceled", _("Canceled")
    EXPIRED = "expired", _("Expired")


#: Statuses that still entitle a customer to use the product.
#: ``PAST_DUE`` is included on purpose — a failed card should start a dunning
#: conversation, not sever a business's messaging mid-campaign. Stage 4 decides
#: how long that grace lasts; until then a past-due subscription keeps working.
ENTITLED_STATUSES = frozenset(
    {SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE}
)


class SubscriptionQuerySet(models.QuerySet):
    def entitled(self) -> SubscriptionQuerySet:
        return self.filter(status__in=ENTITLED_STATUSES)

    def for_organization(self, organization) -> SubscriptionQuerySet:
        """Present for symmetry with every other customer-facing queryset."""
        if organization is None:
            return self.none()
        return self.filter(organization=organization)


class Subscription(models.Model):
    """
    One organization's place on one plan, for one billing period.

    One row per organization rather than a history table. What a customer is
    entitled to *now* is the only question the sending path asks, and a
    ``OneToOneField`` makes the wrong answer unrepresentable — there is no way
    to end up with two active subscriptions disagreeing about a limit. Stage 4
    adds invoices, which is where the history belongs.
    """

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
        help_text=_("PROTECT: deleting a plan customers are on would erase what they bought."),
    )
    status = models.CharField(
        max_length=16, choices=SubscriptionStatus.choices, default=SubscriptionStatus.TRIALING
    )

    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField()
    trial_end = models.DateTimeField(null=True, blank=True)

    cancel_at_period_end = models.BooleanField(
        default=False,
        help_text=_("Cancels when the paid period runs out rather than immediately."),
    )
    canceled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SubscriptionQuerySet.as_manager()

    class Meta:
        verbose_name = _("subscription")
        verbose_name_plural = _("subscriptions")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(current_period_end__gt=models.F("current_period_start")),
                name="subscription_period_is_forward",
            )
        ]
        indexes = [
            models.Index(fields=["status", "current_period_end"], name="subscription_renewal_idx")
        ]

    def __str__(self) -> str:
        return f"{self.organization.name} on {self.plan.name}"

    def save(self, *args, **kwargs):
        if not self.current_period_end:
            self.current_period_end = self.next_period_end()
        super().save(*args, **kwargs)

    def clean(self):
        if self.current_period_end and self.current_period_start:
            if self.current_period_end <= self.current_period_start:
                raise ValidationError({"current_period_end": _("The period must move forward.")})

    # -- Period arithmetic --------------------------------------------------

    def next_period_end(self, start: datetime | None = None) -> datetime:
        start = start or self.current_period_start or timezone.now()
        if self.plan_id and self.plan.interval == PlanInterval.YEARLY:
            return add_months(start, 12)
        return add_months(start, 1)

    @property
    def is_entitled(self) -> bool:
        """Whether this subscription currently permits use of the product."""
        return self.status in ENTITLED_STATUSES

    @property
    def is_trialing(self) -> bool:
        return self.status == SubscriptionStatus.TRIALING

    @property
    def has_expired(self) -> bool:
        return self.current_period_end <= timezone.now()

    @property
    def days_remaining(self) -> int:
        return max(0, (self.current_period_end - timezone.now()).days)


class UsageSnapshot(models.Model):
    """
    What one organization used in one closed billing period.

    Live usage is *counted from the messages themselves* rather than read from a
    running total here — see :mod:`billing.usage`. A counter incremented on send
    can drift from reality after a retry, a crash between the send and the
    increment, or a bulk correction, and a billing number that quietly disagrees
    with the message log is the worst kind of wrong.

    Snapshots exist for the opposite reason: once a period closes, its total must
    stop moving even though the rows it was derived from are still subject to
    retention. This is the record an invoice is written against.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="usage_snapshots"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="usage_snapshots")

    period_start = models.DateTimeField()
    period_end = models.DateTimeField()

    messages_sent = models.PositiveIntegerField(default=0)
    contacts_at_close = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        verbose_name = _("usage snapshot")
        verbose_name_plural = _("usage snapshots")
        constraints = [
            # Closing the same period twice would double-bill it.
            models.UniqueConstraint(
                fields=["organization", "period_start"], name="unique_usage_period"
            )
        ]

    def __str__(self) -> str:
        return f"{self.organization.name}: {self.messages_sent} in {self.period_start:%b %Y}"


def default_trial_end(plan: Plan, start: datetime | None = None) -> datetime | None:
    start = start or timezone.now()
    return start + timedelta(days=plan.trial_days) if plan.trial_days else None
