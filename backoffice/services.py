"""
The cross-tenant reads, all in one file on purpose.

Everywhere else in this project, a query without an organization filter is a
bug. Here it is the point — so rather than scatter unscoped queries through a
views module where they would look exactly like the mistakes they resemble,
every one of them lives here, where the file's name and this docstring say what
they are.

If you are reviewing a change that adds an unscoped query anywhere else, it is
almost certainly wrong. If you are reviewing one here, check instead that it
returns *aggregates and metadata* rather than anybody's content: this module may
count a customer's messages and report their statuses, and may not read what
they said.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from billing.invoicing import Invoice, InvoiceStatus, Payment, PaymentStatus
from billing.models import Plan, Subscription, SubscriptionStatus
from campaigns.models import Campaign
from contacts.models import Contact
from messaging.models import Message, MessageStatus
from organizations.models import Organization, OrganizationStatus
from whatsapp.accounts import MessagingAccount
from whatsapp.models import WebhookEvent, WebhookEventStatus

logger = logging.getLogger(__name__)

#: The window the overview's activity figures cover.
RECENT_DAYS = 30


def platform_overview() -> dict:
    """
    The numbers on the front page. Counts only — no customer is named.

    Deliberately not cached. These are read by a handful of staff a few times a
    day, and a stale figure on an operations dashboard is worse than a slow one:
    somebody makes a decision on it.
    """
    since = timezone.now() - timedelta(days=RECENT_DAYS)

    organizations = Organization.objects.all()
    subscriptions = Subscription.objects.all()
    invoices = Invoice.objects.all()

    return {
        "organizations": {
            "total": organizations.count(),
            "active": organizations.filter(status=OrganizationStatus.ACTIVE).count(),
            "new_this_period": organizations.filter(created_at__gte=since).count(),
        },
        "subscriptions": {
            "trialing": subscriptions.filter(status=SubscriptionStatus.TRIALING).count(),
            "active": subscriptions.filter(status=SubscriptionStatus.ACTIVE).count(),
            "past_due": subscriptions.filter(status=SubscriptionStatus.PAST_DUE).count(),
            "cancelling": subscriptions.filter(cancel_at_period_end=True).count(),
            "missing": organizations.filter(subscription__isnull=True).count(),
        },
        "messages": {
            "sent": Message.objects.filter(sent_at__gte=since).count(),
            "failed": Message.objects.filter(
                failed_at__gte=since, status=MessageStatus.FAILED
            ).count(),
        },
        "money": {
            # Sum of what is owed, not of what has been earned. An "outstanding"
            # figure that quietly included paid invoices would be read as
            # revenue by whoever saw it next.
            "outstanding": invoices.filter(status=InvoiceStatus.OPEN).aggregate(
                total=Sum("total")
            )["total"],
            "outstanding_count": invoices.filter(status=InvoiceStatus.OPEN).count(),
            "overdue_count": invoices.overdue().count(),
            "paid_this_period": invoices.filter(
                status=InvoiceStatus.PAID, paid_at__gte=since
            ).aggregate(total=Sum("total"))["total"],
        },
        "since": since,
    }


def plan_distribution() -> list[dict]:
    """How many customers are on each plan, including the plans nobody chose."""
    return [
        {
            "plan": plan,
            "count": plan.subscriptions.count(),
            "active": plan.subscriptions.filter(status=SubscriptionStatus.ACTIVE).count(),
        }
        for plan in Plan.objects.all().prefetch_related("subscriptions")
    ]


def organizations_with_context(search: str = "", status: str = ""):
    """
    Every customer, annotated with the figures the list column headings promise.

    Annotated rather than fetched per row: a hundred organizations at four
    lookups each is four hundred queries, and an operations page that takes ten
    seconds is one nobody opens.
    """
    queryset = (
        Organization.objects.all()
        .select_related("owner", "subscription__plan")
        .annotate(
            member_count=Count("memberships", distinct=True),
            contact_count=Count("contacts", distinct=True),
            campaign_count=Count("campaigns", distinct=True),
        )
        .order_by("-created_at")
    )

    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(slug__icontains=search)
            | Q(owner__email__icontains=search)
        )

    if status:
        queryset = queryset.filter(status=status)

    return queryset


def organization_detail(organization) -> dict:
    """
    One customer's account, as an operator needs to see it.

    **Metadata and counts only.** Campaign names and message statuses are here;
    message bodies, rendered payloads and contact phone numbers are not. The
    line is that support can answer "did their campaign go out?" without being
    able to read what it said.
    """
    from billing import usage

    since = timezone.now() - timedelta(days=RECENT_DAYS)

    return {
        "organization": organization,
        "usage": usage.summary(organization),
        "members": organization.memberships.select_related("user").order_by("created_at"),
        "senders": MessagingAccount.objects.filter(organization=organization),
        "invoices": Invoice.objects.filter(organization=organization).select_related("plan")[:10],
        "campaigns": (
            Campaign.objects.filter(organization=organization)
            .only("id", "name", "status", "total_recipients", "created_at")
            .order_by("-created_at")[:10]
        ),
        "counts": {
            "contacts": Contact.objects.filter(organization=organization).count(),
            "opted_in": Contact.objects.filter(organization=organization, opted_in=True).count(),
            "campaigns": Campaign.objects.filter(organization=organization).count(),
            "sent_recently": Message.objects.filter(
                organization=organization, sent_at__gte=since
            ).count(),
            "failed_recently": Message.objects.filter(
                organization=organization, failed_at__gte=since, status=MessageStatus.FAILED
            ).count(),
        },
        "since": since,
    }


def health() -> dict:
    """
    What needs a human. Everything here is something somebody should act on.

    Ordered by how quiet the failure is. A past-due subscription announces
    itself to the customer eventually; a webhook that has been failing for three
    days announces itself to nobody at all, and every hour it keeps failing is
    another hour of delivery reports quietly going missing.
    """
    since = timezone.now() - timedelta(days=7)

    return {
        "webhooks_failed": WebhookEvent.objects.filter(
            status=WebhookEventStatus.FAILED, created_at__gte=since
        ).count(),
        "webhooks_rejected": WebhookEvent.objects.filter(
            status=WebhookEventStatus.REJECTED, created_at__gte=since
        ).count(),
        "payments_failed": Payment.objects.filter(
            status=PaymentStatus.FAILED, created_at__gte=since
        ).count(),
        "subscriptions_past_due": Subscription.objects.filter(
            status=SubscriptionStatus.PAST_DUE
        ).select_related("organization", "plan"),
        "invoices_overdue": Invoice.objects.overdue().select_related("organization"),
        "organizations_without_subscription": Organization.objects.filter(
            subscription__isnull=True
        ),
        "senders_unverified": MessagingAccount.objects.exclude(status="active").select_related(
            "organization"
        ),
        "since": since,
    }
