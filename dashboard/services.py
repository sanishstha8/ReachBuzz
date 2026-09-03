"""
Reporting and monitoring aggregates.

Every figure the dashboard, the reports page and the reporting API show is
computed here, once, so the HTML and the JSON can never disagree about what
"delivered" means. Two conventions hold throughout:

* **The status buckets are disjoint.** ``pending``, ``sent``, ``delivered``,
  ``read`` and ``failed`` partition the messages in a period, so they can be
  stacked in a chart and summed to a total without double counting. Where a
  cumulative figure is wanted instead — everything the provider accepted,
  whatever happened next — it is named ``reached``.

* **A message belongs to the day it was created**, which is the day its
  campaign was launched. Grouping by ``sent_at`` would move rows between days
  as retries land, and a report whose past changes underneath the reader is
  worse than one with a stated convention.

Rate arithmetic is not repeated here: per-campaign figures reuse
:class:`messaging.services.CampaignStats`, which the campaign monitoring page
already renders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from campaigns.models import Campaign, CampaignStatus
from contacts.models import Contact, ContactStatus
from messaging.models import Message, MessageStatus
from messaging.services import CampaignStats, FailureReason, failure_breakdown

logger = logging.getLogger(__name__)

# A longer period makes the chart unreadable and the queries slow, and no
# operator has asked a question that needs one.
MAX_PERIOD_DAYS = 366
DEFAULT_PERIOD_DAYS = 30

# Offered in the period picker. The numbers are what appears in ?days=.
PERIOD_PRESETS: tuple[tuple[int, str], ...] = (
    (7, "Last 7 days"),
    (30, "Last 30 days"),
    (90, "Last 90 days"),
    (365, "Last 12 months"),
)


# ---------------------------------------------------------------------------
# The period
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportPeriod:
    """
    An inclusive range of local dates.

    Held as dates rather than datetimes because that is what a reader means by
    "August". The half-open datetime bounds used for querying are derived from
    them in the active timezone.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("A reporting period cannot end before it starts.")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def start_at(self) -> datetime:
        return start_of_day(self.start)

    @property
    def end_at(self) -> datetime:
        """Exclusive upper bound: midnight at the start of the following day."""
        return start_of_day(self.end + timedelta(days=1))

    @property
    def label(self) -> str:
        if self.start == self.end:
            return self.start.strftime("%d %b %Y")
        return f"{self.start.strftime('%d %b %Y')} to {self.end.strftime('%d %b %Y')}"

    @property
    def slug(self) -> str:
        """Filename-safe identity, used by the CSV downloads."""
        return f"{self.start.isoformat()}-to-{self.end.isoformat()}"

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range(self.days)]

    @classmethod
    def last_days(cls, days: int = DEFAULT_PERIOD_DAYS) -> ReportPeriod:
        days = max(1, min(int(days), MAX_PERIOD_DAYS))
        today = timezone.localdate()
        return cls(start=today - timedelta(days=days - 1), end=today)


def start_of_day(day: date) -> datetime:
    """Midnight local time on ``day``, as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, datetime.min.time()))


def resolve_period(params) -> ReportPeriod:
    """
    Build a period from query parameters, falling back rather than failing.

    Accepts ``?days=N``, or an explicit ``?start=YYYY-MM-DD&end=YYYY-MM-DD``.
    A malformed value yields the default period: a report page that raises on a
    hand-edited URL is worse than one that shows the last 30 days.
    """
    start = _parse_date(params.get("start"))
    end = _parse_date(params.get("end"))

    if start or end:
        if start and end and end >= start:
            if (end - start).days + 1 > MAX_PERIOD_DAYS:
                start = end - timedelta(days=MAX_PERIOD_DAYS - 1)
            return ReportPeriod(start=start, end=end)
        if start and not end:
            return ReportPeriod(start=start, end=max(start, timezone.localdate()))
        if end and not start:
            return ReportPeriod(start=end - timedelta(days=DEFAULT_PERIOD_DAYS - 1), end=end)
        logger.debug("Ignoring a reversed report range: start=%s end=%s", start, end)

    days_raw = (params.get("days") or "").strip()
    if days_raw:
        try:
            return ReportPeriod.last_days(int(days_raw))
        except (TypeError, ValueError):
            logger.debug("Ignoring unparseable days=%r on a report request", days_raw)

    return ReportPeriod.last_days()


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Status buckets
# ---------------------------------------------------------------------------

# The disjoint buckets, in the order they are stacked and tabulated.
BUCKETS: tuple[tuple[str, str], ...] = (
    ("read", "Read"),
    ("delivered", "Delivered"),
    ("sent", "Sent"),
    ("pending", "Pending"),
    ("failed", "Failed"),
)

# Which raw statuses roll up into each bucket. "Pending" gathers all three
# in-flight statuses: the difference between queued and sending is a worker's
# business, not a reader's.
BUCKET_STATUSES: dict[str, tuple[str, ...]] = {
    "pending": (MessageStatus.PENDING, MessageStatus.QUEUED, MessageStatus.SENDING),
    "sent": (MessageStatus.SENT,),
    "delivered": (MessageStatus.DELIVERED,),
    "read": (MessageStatus.READ,),
    "failed": (MessageStatus.FAILED,),
}


def _bucket_aggregates() -> dict[str, Count]:
    return {
        bucket: Count("id", filter=Q(status__in=statuses))
        for bucket, statuses in BUCKET_STATUSES.items()
    }


@dataclass(frozen=True)
class DayActivity:
    """One column of the activity chart."""

    day: date
    pending: int = 0
    sent: int = 0
    delivered: int = 0
    read: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.sent + self.delivered + self.read + self.failed

    @property
    def reached(self) -> int:
        """Everything the provider accepted, whatever happened afterwards."""
        return self.sent + self.delivered + self.read

    def value(self, bucket: str) -> int:
        return getattr(self, bucket)


def daily_activity(organization, period: ReportPeriod) -> list[DayActivity]:
    """
    Message outcomes per day, with quiet days present as zeros.

    The gaps matter: a chart that silently closes up days on which nothing was
    sent implies a steadier send rate than actually happened.
    """
    rows = (
        Message.objects.for_organization(organization)
        .filter(created_at__gte=period.start_at, created_at__lt=period.end_at)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(**_bucket_aggregates())
        .order_by("day")
    )
    by_day = {row["day"]: row for row in rows}

    activity: list[DayActivity] = []
    for day in period.dates():
        row = by_day.get(day)
        if row is None:
            activity.append(DayActivity(day=day))
            continue
        activity.append(
            DayActivity(
                day=day,
                pending=row["pending"],
                sent=row["sent"],
                delivered=row["delivered"],
                read=row["read"],
                failed=row["failed"],
            )
        )
    return activity


# ---------------------------------------------------------------------------
# Headline figures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Overview:
    """The numbers at the top of the reports page."""

    period: ReportPeriod
    messages: int = 0
    pending: int = 0
    sent: int = 0
    delivered: int = 0
    read: int = 0
    failed: int = 0
    campaigns_launched: int = 0
    recipients: int = 0
    contacts_added: int = 0
    opt_ins: int = 0
    opt_outs: int = 0

    @property
    def reached(self) -> int:
        return self.sent + self.delivered + self.read

    @property
    def confirmed_delivered(self) -> int:
        """Delivered plus read: a read message was necessarily delivered."""
        return self.delivered + self.read

    @property
    def delivery_rate(self) -> float:
        """
        Share of accepted messages the provider confirmed reaching a handset.

        Measured against ``reached`` rather than against every message, because
        a message still queued has not failed to arrive — it has not been sent
        yet, and counting it as a miss would understate a live campaign.
        """
        if not self.reached:
            return 0.0
        return round(self.confirmed_delivered / self.reached * 100, 1)

    @property
    def read_rate(self) -> float:
        if not self.confirmed_delivered:
            return 0.0
        return round(self.read / self.confirmed_delivered * 100, 1)

    @property
    def failure_rate(self) -> float:
        if not self.messages:
            return 0.0
        return round(self.failed / self.messages * 100, 1)

    @property
    def net_consent_change(self) -> int:
        return self.opt_ins - self.opt_outs


def overview(organization, period: ReportPeriod) -> Overview:
    """Everything the headline tiles need, in three queries."""
    message_counts = Message.objects.for_organization(organization).filter(
        created_at__gte=period.start_at, created_at__lt=period.end_at
    ).aggregate(messages=Count("id"), **_bucket_aggregates())

    campaign_counts = Campaign.objects.for_organization(organization).filter(
        started_at__gte=period.start_at, started_at__lt=period.end_at
    ).aggregate(launched=Count("id", distinct=True), recipients=Count("messages"))

    contact_counts = Contact.objects.for_organization(organization).aggregate(
        added=Count("id", filter=Q(created_at__gte=period.start_at, created_at__lt=period.end_at)),
        opt_ins=Count("id", filter=Q(opt_in_at__gte=period.start_at, opt_in_at__lt=period.end_at)),
        opt_outs=Count(
            "id", filter=Q(opt_out_at__gte=period.start_at, opt_out_at__lt=period.end_at)
        ),
    )

    return Overview(
        period=period,
        messages=message_counts["messages"],
        pending=message_counts["pending"],
        sent=message_counts["sent"],
        delivered=message_counts["delivered"],
        read=message_counts["read"],
        failed=message_counts["failed"],
        campaigns_launched=campaign_counts["launched"],
        recipients=campaign_counts["recipients"],
        contacts_added=contact_counts["added"],
        opt_ins=contact_counts["opt_ins"],
        opt_outs=contact_counts["opt_outs"],
    )


# ---------------------------------------------------------------------------
# Per-campaign performance
# ---------------------------------------------------------------------------


def _campaign_status_annotations() -> dict[str, Count]:
    """Per-status message counts for a campaign queryset, in one query."""
    return {
        f"{status}_count": Count("messages", filter=Q(messages__status=status), distinct=True)
        for status, _label in MessageStatus.choices
    }


def _stats_from_annotations(campaign: Campaign) -> CampaignStats:
    """Rebuild the shared stats object from an annotated campaign row."""
    counts = {status: getattr(campaign, f"{status}_count") for status, _ in MessageStatus.choices}
    return CampaignStats(total=sum(counts.values()), **counts)


@dataclass(frozen=True)
class CampaignRow:
    """
    One line of the campaign performance table.

    Deliberately a pairing rather than a flattened record: the rates are
    ``CampaignStats``' job, and duplicating that arithmetic here is how the
    reports page and the monitoring page would drift apart.
    """

    campaign: Campaign
    stats: CampaignStats

    @property
    def name(self) -> str:
        return self.campaign.name

    @property
    def status(self) -> str:
        return self.campaign.status

    @property
    def status_label(self) -> str:
        return self.campaign.get_status_display()

    @property
    def template_name(self) -> str:
        return self.campaign.template.name if self.campaign.template_id else ""


def campaign_performance(
    organization, period: ReportPeriod, *, limit: int | None = None
) -> list[CampaignRow]:
    """
    Campaigns *launched* in the period, with their delivery outcomes.

    Drafts are deliberately absent: this table reports on sends that happened,
    and a campaign that was never launched has no performance to report.
    """
    queryset = (
        Campaign.objects.for_organization(organization)
        .filter(started_at__gte=period.start_at, started_at__lt=period.end_at)
        .select_related("template")
        .annotate(**_campaign_status_annotations())
        .order_by("-started_at")
    )
    if limit is not None:
        queryset = queryset[:limit]

    return [CampaignRow(campaign=c, stats=_stats_from_annotations(c)) for c in queryset]


def active_campaigns(organization) -> list[CampaignRow]:
    """
    Campaigns sending right now, for the dashboard's live panel.

    Paused campaigns are included: an operator watching a send needs to see the
    one they just stopped, not lose it from the panel the moment they stop it.
    """
    queryset = (
        Campaign.objects.for_organization(organization)
        .filter(status__in=[CampaignStatus.PROCESSING, CampaignStatus.PAUSED])
        .select_related("template")
        .annotate(**_campaign_status_annotations())
        .order_by("-started_at", "-created_at")
    )
    return [CampaignRow(campaign=c, stats=_stats_from_annotations(c)) for c in queryset]


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def failure_reasons(organization, period: ReportPeriod, *, limit: int = 20) -> list[FailureReason]:
    """
    Why messages failed in the period, most common first.

    The grouping itself belongs to ``messaging``: it is a fact about messages,
    and the campaign monitoring page asks for the same breakdown scoped to one
    campaign. All this adds is the period.
    """
    return failure_breakdown(
        Message.objects.for_organization(organization).filter(
            created_at__gte=period.start_at, created_at__lt=period.end_at
        ),
        limit=limit,
        per_campaign=True,
    )


def recent_failures(organization, *, limit: int = 8) -> list[Message]:
    """The newest failed messages across every campaign."""
    return list(
        Message.objects.for_organization(organization)
        .filter(status=MessageStatus.FAILED)
        .select_related("contact", "campaign")
        .order_by("-failed_at", "-created_at")[:limit]
    )


# ---------------------------------------------------------------------------
# Audience and consent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsentSummary:
    """The compliance picture: who may be messaged, and on what basis."""

    total: int = 0
    opted_in: int = 0
    eligible: int = 0
    opted_out: int = 0
    by_opt_in_source: tuple[tuple[str, int], ...] = ()
    by_status: tuple[tuple[str, int], ...] = ()

    @property
    def opt_in_rate(self) -> float:
        if not self.total:
            return 0.0
        return round(self.opted_in / self.total * 100, 1)


def consent_summary(organization) -> ConsentSummary:
    """
    Current consent state. Deliberately not a period figure.

    Consent is a state, not an event: "how many people may we message" is only
    answerable as of now, so this ignores the report period rather than
    implying a historical answer it cannot give.
    """
    from contacts.models import OptInSource

    # Aliases must not reuse a field name, or Django resolves the filter's
    # reference to the alias and refuses to aggregate over an aggregate.
    totals = Contact.objects.for_organization(organization).aggregate(
        total_count=Count("id"),
        opted_in_count=Count("id", filter=Q(opted_in=True)),
        eligible_count=Count("id", filter=Q(opted_in=True, status=ContactStatus.ACTIVE)),
        opted_out_count=Count("id", filter=Q(opted_in=False)),
    )

    source_labels = dict(OptInSource.choices)
    by_source = tuple(
        (str(source_labels.get(row["opt_in_source"]) or "Not recorded"), row["count"])
        for row in Contact.objects.for_organization(organization)
        .filter(opted_in=True)
        .values("opt_in_source")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    status_labels = dict(ContactStatus.choices)
    by_status = tuple(
        (str(status_labels.get(row["status"], row["status"])), row["count"])
        for row in Contact.objects.for_organization(organization)
        .values("status")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    return ConsentSummary(
        total=totals["total_count"],
        opted_in=totals["opted_in_count"],
        eligible=totals["eligible_count"],
        opted_out=totals["opted_out_count"],
        by_opt_in_source=by_source,
        by_status=by_status,
    )
