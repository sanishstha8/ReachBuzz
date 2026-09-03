"""
Chart geometry.

The project has no frontend build step and no charting library, so charts are
inline SVG. Rather than compute coordinates inside a Django template — where
arithmetic is painful and untestable — every number the SVG needs is worked out
here and handed over as plain data. The template only iterates.

**Four bands, not five.** The data layer keeps five disjoint buckets, and the
API and the CSV exports still report all five. The chart folds ``read`` into
``delivered``, because a read message was necessarily delivered and the chart's
question is *how much went out and did it land*. How much of it was read is a
different question, and the read-rate tile answers it better than a fifth
stacked band ever did.

This started as a single-hue ordinal ramp — the lifecycle is an ordered
sequence, so in principle the reader should see the order in the colour. In
practice four steps of one hue were four shades of teal, and the validator
agreed: read against delivered measured ΔE 12.0, below the 15 floor at which
colours are "hard to tell apart even with full colour vision". An ordering the
reader cannot see is not an ordering.

So the bands are now distinct hues, chosen for what they mean rather than for
where they sit in a sequence, and validated as a set against the white card
surface: worst adjacent pair ΔE 16.8 normal vision, 14.2 under simulated
colour-vision deficiency. ``pending`` is deliberately the one neutral. It means
*nothing has happened yet*, so it should recede rather than compete for
attention — it is the one slot that fails the chroma floor on purpose, since
that floor exists to keep a hue doing identity work and this one is not meant
to. Both it and the table view are why every chart also prints its totals.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date

from dashboard.services import DayActivity

# -- Palette ----------------------------------------------------------------
#
# Four steps of one teal ramp for the lifecycle, plus the reserved critical
# red. Text never wears these: labels use the muted ink below.

SERIES_COLOURS: dict[str, str] = {
    "delivered": "#0e8f5b",  # green: confirmed on a handset
    "sent": "#2a78d6",  # blue: the provider took it, nothing has confirmed it
    "pending": "#8b97a5",  # neutral: nothing has happened yet
    "failed": "#d03b3b",  # the reserved critical red
}

# Which data buckets each visible band is made of, bottom of the stack first.
# The good outcomes sit on the baseline where they are easiest to compare, and
# failures ride on top, where a bad day shows along the skyline.
SERIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("delivered", "Delivered", ("read", "delivered")),
    ("sent", "Sent", ("sent",)),
    ("pending", "Pending", ("pending",)),
    ("failed", "Failed", ("failed",)),
)

# Chart *chrome* — gridlines, axis, tick labels — is styled in app.css, not
# here. Only the series colours are data: the legend swatches and the tooltip
# keys need the same values the marks are painted with.

# -- Geometry ---------------------------------------------------------------

WIDTH = 760
HEIGHT = 240
# The left gutter holds the y-axis tick labels: wide enough for a seven-glyph
# figure like "100,000" at 10px. The right one keeps the last date label, which
# is centred on its column, from being clipped by the edge of the viewBox.
PAD_LEFT = 56
PAD_RIGHT = 20
PAD_TOP = 12
PAD_BOTTOM = 28

# Marks stay thin: a column never fills its slot, and the leftover is air.
MAX_BAR_WIDTH = 24.0
# The surface, not a stroke, is what separates touching marks.
SEGMENT_GAP = 2.0
CORNER_RADIUS = 4.0

# Past this many columns the bars are thinner than the gap between them, so
# days are aggregated into wider buckets instead.
MAX_COLUMNS = 62
# At most this many x-axis labels, or they collide.
MAX_X_LABELS = 10
# Print the total on a column's cap only while at most this many columns have
# anything in them. Past that the labels crowd each other and the axis, and
# the tooltip and the table view carry the values instead.
MAX_VALUE_LABELS = 12

PLOT_LEFT = float(PAD_LEFT)
PLOT_RIGHT = float(WIDTH - PAD_RIGHT)
PLOT_TOP = float(PAD_TOP)
PLOT_BOTTOM = float(HEIGHT - PAD_BOTTOM)
PLOT_WIDTH = PLOT_RIGHT - PLOT_LEFT
PLOT_HEIGHT = PLOT_BOTTOM - PLOT_TOP


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    """One column's worth of days, already summed."""

    start: date
    end: date
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def is_range(self) -> bool:
        return self.end > self.start

    @property
    def label(self) -> str:
        """Full label, used by the tooltip and the table view."""
        if not self.is_range:
            return self.start.strftime("%d %b %Y")
        return f"{self.start.strftime('%d %b')} to {self.end.strftime('%d %b %Y')}"

    @property
    def short_label(self) -> str:
        """Axis label: no year, because the axis is already one period."""
        return self.start.strftime("%d %b")


def bucket_activity(activity: list[DayActivity], *, max_columns: int = MAX_COLUMNS) -> list[Bucket]:
    """
    Group days into at most ``max_columns`` columns.

    Chunked from the most recent day backwards, so any partial bucket is the
    *oldest* one. Chunking the other way would leave the newest column covering
    fewer days than the rest — and that is the column a reader looks at first,
    so it is the one that must not understate itself.
    """
    if not activity:
        return []

    size = max(1, math.ceil(len(activity) / max_columns))
    buckets: list[Bucket] = []

    # Walk backwards in fixed-size chunks, then restore chronological order.
    end_index = len(activity)
    while end_index > 0:
        start_index = max(0, end_index - size)
        chunk = activity[start_index:end_index]
        buckets.append(
            Bucket(
                start=chunk[0].day,
                end=chunk[-1].day,
                counts={
                    key: sum(day.value(bucket) for day in chunk for bucket in members)
                    for key, _label, members in SERIES
                },
            )
        )
        end_index = start_index

    buckets.reverse()
    return buckets


# ---------------------------------------------------------------------------
# Axis scaling
# ---------------------------------------------------------------------------


def axis_scale(raw_max: int) -> tuple[int, int]:
    """
    Choose a tick interval and a tick count for a count axis.

    Returns ``(step, divisions)``, so the axis tops out at ``step * divisions``.
    Both are integers, because the axis counts messages and a gridline labelled
    "2.5 messages" is nonsense — which is also why this is not the usual
    1/2/5 ladder alone: with a fixed four divisions, a maximum of 10 would round
    up to an axis of 20 and leave every column at half height. Varying the
    number of divisions as well as the step keeps the columns filling the plot.
    """
    if raw_max <= 0:
        return 1, 2

    best: tuple[int, int] | None = None
    exponent = max(0, math.floor(math.log10(raw_max)) - 1)
    for power in range(exponent, exponent + 3):
        for multiple in (1, 2, 2.5, 5):
            step = multiple * 10**power
            if step != int(step):
                continue
            step = int(step)
            for divisions in (4, 5, 3, 2):
                if step * divisions < raw_max:
                    continue
                if best is None or step * divisions < best[0] * best[1]:
                    best = (step, divisions)

    return best or (raw_max, 1)


@dataclass(frozen=True)
class GridLine:
    y: float
    value: int
    label: str


# ---------------------------------------------------------------------------
# The chart
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """
    One coloured part of a stacked column.

    Carries its geometry as well as the path drawn from it: ``path`` is an
    opaque string once built, and the numbers are what a test — or a future
    second renderer — actually needs.
    """

    key: str
    label: str
    value: int
    colour: str
    y: float
    height: float
    path: str


@dataclass(frozen=True)
class Column:
    bucket: Bucket
    x: float
    centre_x: float
    width: float
    hit_x: float
    hit_width: float
    segments: list[Segment]
    label: str
    short_label: str
    total: int
    show_label: bool
    # A value printed on the cap of the column. Only when few enough columns
    # carry one to stay readable — a number on every bar is chaos and goes
    # unread, which is the whole reason direct labels work when they are rare.
    show_value: bool
    value_text: str
    value_y: float
    description: str
    # Serialised for the hover/focus tooltip. Built here rather than in the
    # template so the values reach the DOM as data, not as concatenated markup.
    tooltip_json: str
    # Every bucket including the empty ones, in legend order, for the table
    # view — which is how a reader gets the numbers without hovering.
    table_cells: list[int]


@dataclass(frozen=True)
class LegendItem:
    key: str
    label: str
    colour: str
    total: int


@dataclass(frozen=True)
class StackedColumnChart:
    """Everything the SVG template needs, and nothing it has to compute."""

    columns: list[Column] = field(default_factory=list)
    grid_lines: list[GridLine] = field(default_factory=list)
    legend: list[LegendItem] = field(default_factory=list)
    max_value: int = 0
    total: int = 0
    width: int = WIDTH
    height: int = HEIGHT
    plot_left: float = PLOT_LEFT
    plot_right: float = PLOT_RIGHT
    plot_top: float = PLOT_TOP
    plot_bottom: float = PLOT_BOTTOM
    plot_height: float = PLOT_HEIGHT
    # Where the y-axis tick labels and the x-axis date labels sit. Precomputed
    # for the same reason as everything else here: Django's ``add`` filter
    # truncates floats, so arithmetic in the template would quietly misplace them.
    tick_label_x: float = PLOT_LEFT - 8
    x_label_y: float = HEIGHT - 9
    summary: str = ""

    @property
    def has_data(self) -> bool:
        return self.total > 0


def _rounded_top_path(x: float, y: float, width: float, height: float, radius: float) -> str:
    """
    A rectangle with a rounded top edge and a square bottom.

    Stacked segments sit on one another, so only the cap of the column should
    be rounded; ``rx`` on a rect would round the bottom corners too and leave a
    visible notch above the segment underneath.
    """
    radius = max(0.0, min(radius, width / 2, height))
    bottom = y + height
    if radius <= 0:
        return f"M{x:.2f},{y:.2f} H{x + width:.2f} V{bottom:.2f} H{x:.2f} Z"
    return (
        f"M{x:.2f},{bottom:.2f} "
        f"V{y + radius:.2f} "
        f"Q{x:.2f},{y:.2f} {x + radius:.2f},{y:.2f} "
        f"H{x + width - radius:.2f} "
        f"Q{x + width:.2f},{y:.2f} {x + width:.2f},{y + radius:.2f} "
        f"V{bottom:.2f} Z"
    )


def stacked_column_chart(activity: list[DayActivity]) -> StackedColumnChart:
    """
    Build a stacked column chart of message outcomes over time.

    The stack runs darkest at the baseline to lightest above it — the ordinal
    ramp's own order — with failures in critical red on top, where a bad day is
    visible along the skyline rather than buried in the middle of the stack.
    """
    buckets = bucket_activity(activity)
    labels = {key: label for key, label, _members in SERIES}

    totals_by_key = {
        key: sum(bucket.counts.get(key, 0) for bucket in buckets) for key, _label, _m in SERIES
    }
    grand_total = sum(totals_by_key.values())
    legend = [
        LegendItem(key=key, label=label, colour=SERIES_COLOURS[key], total=totals_by_key[key])
        for key, label, _members in SERIES
    ]

    if not buckets or grand_total == 0:
        return StackedColumnChart(
            legend=legend,
            summary="No messages were created in this period.",
        )

    step, divisions = axis_scale(max(bucket.total for bucket in buckets))
    max_value = step * divisions
    grid_lines = [
        GridLine(
            y=PLOT_BOTTOM - (step * index / max_value) * PLOT_HEIGHT,
            value=step * index,
            label=f"{step * index:,}",
        )
        for index in range(divisions + 1)
    ]

    slot = PLOT_WIDTH / len(buckets)
    bar_width = max(1.0, min(MAX_BAR_WIDTH, slot * 0.8))
    stride = max(1, math.ceil(len(buckets) / MAX_X_LABELS))

    occupied = [bucket for bucket in buckets if bucket.total]
    label_values = len(occupied) <= MAX_VALUE_LABELS

    columns: list[Column] = []
    for index, bucket in enumerate(buckets):
        slot_x = PLOT_LEFT + slot * index
        x = slot_x + (slot - bar_width) / 2

        segments: list[Segment] = []
        # Find the topmost non-empty bucket so only its cap is rounded.
        stacked_keys = [key for key, _l, _m in SERIES if bucket.counts.get(key, 0)]
        top_key = stacked_keys[-1] if stacked_keys else None

        cursor = PLOT_BOTTOM
        for key, label, _members in SERIES:
            value = bucket.counts.get(key, 0)
            if not value:
                continue

            full_height = (value / max_value) * PLOT_HEIGHT
            # The surface gap comes out of the segment, never out of the total,
            # so the column's overall height still reads as its true value.
            height = max(1.0, full_height - SEGMENT_GAP)
            y = cursor - full_height
            cursor -= full_height

            segments.append(
                Segment(
                    key=key,
                    label=label,
                    value=value,
                    colour=SERIES_COLOURS[key],
                    y=y,
                    height=height,
                    path=_rounded_top_path(
                        x, y, bar_width, height, CORNER_RADIUS if key == top_key else 0.0
                    ),
                )
            )

        parts = [
            f"{labels[key]} {bucket.counts.get(key, 0):,}"
            for key, _l, _m in SERIES
            if bucket.counts.get(key, 0)
        ]
        columns.append(
            Column(
                bucket=bucket,
                x=x,
                centre_x=x + bar_width / 2,
                width=bar_width,
                hit_x=slot_x,
                hit_width=slot,
                segments=segments,
                label=bucket.label,
                short_label=bucket.short_label,
                total=bucket.total,
                show_label=index % stride == 0 or index == len(buckets) - 1,
                show_value=label_values and bool(bucket.total),
                value_text=f"{bucket.total:,}",
                # Above the cap of the stack, clear of the mark itself.
                value_y=(segments[-1].y - 6) if segments else PLOT_BOTTOM,
                description=f"{bucket.label}: {', '.join(parts)}",
                tooltip_json=json.dumps(
                    {
                        "label": bucket.label,
                        "total": f"{bucket.total:,}",
                        "rows": [
                            {
                                "label": labels[key],
                                "value": f"{bucket.counts.get(key, 0):,}",
                                "colour": SERIES_COLOURS[key],
                            }
                            for key, _l, _m in SERIES
                            if bucket.counts.get(key, 0)
                        ],
                    }
                ),
                table_cells=[bucket.counts.get(key, 0) for key, _l, _m in SERIES],
            )
        )

    first = buckets[0].label
    last = buckets[-1].label
    return StackedColumnChart(
        columns=columns,
        grid_lines=grid_lines,
        legend=legend,
        max_value=max_value,
        total=grand_total,
        summary=(
            f"{grand_total:,} messages between {first} and {last}, "
            f"by outcome. The tallest column holds {max(b.total for b in buckets):,}."
        ),
    )


# ---------------------------------------------------------------------------
# Proportion bar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proportion:
    key: str
    label: str
    value: int
    percent: float
    colour: str


def proportions(counts: dict[str, int]) -> list[Proportion]:
    """
    A single campaign's outcome mix, as percentages of its recipients.

    Used for the thin bar beside each row of the campaign tables, where a full
    chart would be noise but the shape of the outcome still tells the story.
    """
    total = sum(counts.values())
    if not total:
        return []

    return [
        Proportion(
            key=key,
            label=label,
            value=counts[key],
            percent=round(counts[key] / total * 100, 1),
            colour=SERIES_COLOURS[key],
        )
        for key, label, _members in SERIES
        if counts.get(key)
    ]


def stats_proportions(stats) -> list[Proportion]:
    """``proportions()`` for a :class:`messaging.services.CampaignStats`."""
    return proportions(
        {
            # Read folds into delivered here for the same reason it does in the
            # chart: a read message was necessarily delivered, and the bar is
            # about whether the send landed.
            "delivered": stats.delivered + stats.read,
            "sent": stats.sent,
            "pending": stats.in_flight,
            "failed": stats.failed,
        }
    )
