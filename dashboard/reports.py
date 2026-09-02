"""
Downloadable CSV reports.

Four things are true of every report here.

* **It streams.** Rows are generated lazily and written straight to the
  response, so a 20,000-recipient export costs one row of memory rather than
  twenty thousand.

* **It is escaped against spreadsheet formula injection.** Contact names arrive
  from CSV imports and web forms; a cell beginning ``=``, ``+``, ``-`` or ``@``
  is executed as a formula by Excel and Sheets when the file is opened. Text
  cells are prefixed so they stay text. This is why exports are not built by
  handing a queryset straight to ``csv.writer``.

* **It contains no credential.** Reports describe contacts, campaigns and
  provider errors. Nothing here reads a token, and nothing should be added that
  does.

* **It is audited.** An export puts personal data on someone's laptop, which is
  an event the compliance trail should be able to show later. The view records
  it; see :class:`dashboard.views.ReportDownloadView`.

Times are written in the project's configured timezone, matching what the
reports page displays, so a figure on screen and a row in the file cannot
disagree.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.text import slugify

from campaigns.models import Campaign
from contacts.models import Contact
from dashboard import services
from dashboard.services import ReportPeriod
from messaging.models import Message

# Leading characters a spreadsheet treats as the start of a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Rows fetched from the database per round trip while streaming.
CHUNK_SIZE = 2_000


def _text(value: object) -> str:
    """
    Render a cell as text, defused against formula injection.

    Applied to every free-text cell rather than only to the ones that look
    risky today: the rule has to hold for the next field somebody adds.
    """
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def _moment(value: datetime | None) -> str:
    """A timestamp in local time, or an empty cell — never a fabricated one."""
    if value is None:
        return ""
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


class _Echo:
    """A file-like object that returns what it is given, for streaming CSV."""

    def write(self, value: str) -> str:  # noqa: D102 - file protocol
        return value


def stream_csv(filename: str, header: tuple[str, ...], rows: Iterator[list]) -> StreamingHttpResponse:
    """Wrap a row generator in a downloadable, streaming CSV response."""
    writer = csv.writer(_Echo())

    def generate() -> Iterator[str]:
        yield writer.writerow(header)
        for row in rows:
            yield writer.writerow(row)

    response = StreamingHttpResponse(generate(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    # The file is a snapshot of live data; a cached copy would be wrong the
    # moment the next message changes state.
    response["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def campaign_rows(period: ReportPeriod) -> Iterator[list]:
    """One row per campaign launched in the period, with its outcome mix."""
    for row in services.campaign_performance(period):
        stats = row.stats
        yield [
            _text(row.name),
            _text(row.status_label),
            _text(row.template_name),
            _moment(row.campaign.started_at),
            _moment(row.campaign.completed_at),
            stats.total,
            stats.in_flight,
            stats.sent,
            stats.delivered,
            stats.read,
            stats.failed,
            stats.delivery_rate,
            stats.failure_rate,
        ]


def message_rows(period: ReportPeriod) -> Iterator[list]:
    """One row per recipient message created in the period."""
    queryset = (
        Message.objects.filter(created_at__gte=period.start_at, created_at__lt=period.end_at)
        .select_related("campaign", "contact")
        .order_by("created_at", "id")
    )
    yield from _message_rows(queryset)


def campaign_message_rows(campaign: Campaign) -> Iterator[list]:
    """One row per recipient of a single campaign, for its monitoring page."""
    queryset = (
        Message.objects.filter(campaign=campaign)
        .select_related("campaign", "contact")
        .order_by("contact__name", "id")
    )
    yield from _message_rows(queryset)


def _message_rows(queryset) -> Iterator[list]:
    for message in queryset.iterator(chunk_size=CHUNK_SIZE):
        yield [
            _text(message.campaign.name),
            _text(message.contact.name),
            # Prefixed with "+", which a spreadsheet would otherwise evaluate.
            _text(message.to_phone_number),
            _text(message.get_status_display()),
            _text(message.template_name),
            message.attempt_count,
            _text(message.error_code),
            _text(message.error_message),
            _moment(message.created_at),
            _moment(message.sent_at),
            _moment(message.delivered_at),
            _moment(message.read_at),
            _moment(message.failed_at),
        ]


def failure_rows(period: ReportPeriod) -> Iterator[list]:
    """Distinct provider errors in the period, most frequent first."""
    for reason in services.failure_reasons(period, limit=200):
        yield [
            _text(reason.error_code),
            _text(reason.error_message),
            reason.count,
            reason.affected_campaigns,
        ]


def consent_rows(_period: ReportPeriod) -> Iterator[list]:
    """
    The consent register: every contact and the basis on which they may be
    messaged.

    Deliberately not filtered by the report period. Consent is a state, and the
    question this file answers — "who may we message, and how do we know" — is
    only ever answerable as of now.
    """
    queryset = (
        Contact.objects.all()
        .prefetch_related("group_memberships__group")
        .order_by("name", "phone_number")
    )
    for contact in queryset.iterator(chunk_size=CHUNK_SIZE):
        yield [
            _text(contact.name),
            _text(contact.phone_number),
            _text(contact.email),
            _text(contact.get_status_display()),
            "yes" if contact.opted_in else "no",
            "yes" if contact.is_eligible else "no",
            _text(contact.get_opt_in_source_display() if contact.opt_in_source else ""),
            _moment(contact.opt_in_at),
            _text(contact.get_opt_out_source_display() if contact.opt_out_source else ""),
            _moment(contact.opt_out_at),
            _text("; ".join(contact.group_names)),
            _moment(contact.created_at),
        ]


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportSpec:
    """One downloadable report, described once and used by the URL and the UI."""

    slug: str
    label: str
    description: str
    header: tuple[str, ...]
    build: Callable[[ReportPeriod], Iterator[list]]
    # False for reports whose subject is current state rather than a period.
    uses_period: bool = True

    def filename(self, period: ReportPeriod, *, prefix: str) -> str:
        stamp = period.slug if self.uses_period else timezone.localdate().isoformat()
        return f"{prefix}-{self.slug}-{stamp}.csv"


REPORTS: dict[str, ReportSpec] = {
    "campaigns": ReportSpec(
        slug="campaigns",
        label="Campaign performance",
        description="One row per campaign launched in this period, with delivery and failure rates.",
        header=(
            "Campaign",
            "Status",
            "Template",
            "Launched",
            "Completed",
            "Recipients",
            "Pending",
            "Sent",
            "Delivered",
            "Read",
            "Failed",
            "Delivery rate %",
            "Failure rate %",
        ),
        build=campaign_rows,
    ),
    "messages": ReportSpec(
        slug="messages",
        label="Message detail",
        description="One row per recipient message created in this period, with its status timeline.",
        header=(
            "Campaign",
            "Contact",
            "Phone number",
            "Status",
            "Template",
            "Attempts",
            "Error code",
            "Error message",
            "Created",
            "Sent",
            "Delivered",
            "Read",
            "Failed",
        ),
        build=message_rows,
    ),
    "failures": ReportSpec(
        slug="failures",
        label="Failure reasons",
        description="Distinct provider errors in this period and how often each occurred.",
        header=("Error code", "Error message", "Messages", "Campaigns affected"),
        build=failure_rows,
    ),
    "consent": ReportSpec(
        slug="consent",
        label="Consent register",
        description="Every contact and the recorded basis for messaging them. Current state, not a period.",
        header=(
            "Name",
            "Phone number",
            "Email",
            "Status",
            "Opted in",
            "Can be messaged",
            "Opt-in source",
            "Opt-in at",
            "Opt-out source",
            "Opt-out at",
            "Groups",
            "Added",
        ),
        build=consent_rows,
        uses_period=False,
    ),
}

CAMPAIGN_RECIPIENTS_HEADER = REPORTS["messages"].header


def filename_prefix(site_name: str) -> str:
    """A filename-safe prefix from the configured brand name."""
    return slugify(site_name) or "report"
