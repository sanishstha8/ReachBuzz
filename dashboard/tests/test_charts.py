"""
Chart geometry.

Charts are the one place where being subtly wrong is invisible: a bar drawn at
the wrong height still looks like a bar. So the geometry is tested as
arithmetic — segment heights in proportion to their values, columns inside the
plot area, an axis whose ticks are whole messages — rather than by rendering it
and looking.
"""

from __future__ import annotations

from datetime import date, timedelta

from dashboard import charts
from dashboard.services import BUCKETS, DayActivity


def _days(count: int, **counts: int) -> list[DayActivity]:
    """``count`` consecutive days, each with the same outcome counts."""
    start = date(2026, 8, 1)
    return [DayActivity(day=start + timedelta(days=offset), **counts) for offset in range(count)]


class TestAxisScale:
    def test_the_axis_always_covers_the_data(self) -> None:
        for raw_max in list(range(1, 200)) + [1_000, 1_234, 98_765]:
            step, divisions = charts.axis_scale(raw_max)
            assert step * divisions >= raw_max, raw_max

    def test_ticks_are_whole_messages(self) -> None:
        """A gridline reading "2.5 messages" is nonsense."""
        for raw_max in range(1, 200):
            step, divisions = charts.axis_scale(raw_max)
            assert isinstance(step, int)
            assert isinstance(divisions, int)

    def test_the_axis_does_not_dwarf_the_data(self) -> None:
        """Rounding 10 up to an axis of 20 leaves every column at half height."""
        for raw_max in range(2, 200):
            step, divisions = charts.axis_scale(raw_max)
            assert raw_max / (step * divisions) >= 0.7, raw_max

    def test_an_empty_chart_still_has_a_scale(self) -> None:
        assert charts.axis_scale(0) == (1, 2)


class TestBucketing:
    def test_short_periods_keep_one_column_per_day(self) -> None:
        buckets = charts.bucket_activity(_days(30, delivered=1))
        assert len(buckets) == 30
        assert all(not bucket.is_range for bucket in buckets)

    def test_long_periods_are_compressed(self) -> None:
        buckets = charts.bucket_activity(_days(365, delivered=1))
        assert len(buckets) <= charts.MAX_COLUMNS

    def test_compression_preserves_the_totals(self) -> None:
        buckets = charts.bucket_activity(_days(365, delivered=2, failed=1))
        assert sum(bucket.total for bucket in buckets) == 365 * 3

    def test_any_partial_bucket_is_the_oldest_one(self) -> None:
        """
        The newest column is the one a reader looks at first, so it must never
        cover fewer days than the rest and understate itself.
        """
        buckets = charts.bucket_activity(_days(100, delivered=1), max_columns=7)

        widths = [(bucket.end - bucket.start).days + 1 for bucket in buckets]
        assert widths[0] <= widths[-1]
        assert len(set(widths[1:])) == 1

    def test_a_multi_day_bucket_labels_its_range(self) -> None:
        bucket = charts.bucket_activity(_days(14, sent=1), max_columns=2)[0]
        assert bucket.is_range
        assert " to " in bucket.label


class TestStackedColumnChart:
    def test_an_empty_period_reports_no_data_rather_than_a_flat_line(self) -> None:
        chart = charts.stacked_column_chart(_days(7))

        assert chart.has_data is False
        assert chart.columns == []
        # The legend still lists every series, so the reader knows what would
        # have been plotted.
        assert len(chart.legend) == len(BUCKETS)

    def test_segment_heights_are_proportional_to_their_values(self) -> None:
        chart = charts.stacked_column_chart(_days(1, delivered=3, failed=1))
        column = chart.columns[0]

        heights = {segment.key: segment.height for segment in column.segments}

        # Three delivered against one failed, allowing for the 2px surface gap
        # that comes out of each segment rather than out of the column's total.
        assert heights["delivered"] > heights["failed"]
        assert abs(
            (heights["delivered"] + charts.SEGMENT_GAP) / (heights["failed"] + charts.SEGMENT_GAP)
            - 3
        ) < 0.01

    def test_empty_buckets_produce_no_segment(self) -> None:
        chart = charts.stacked_column_chart(_days(1, delivered=5))
        assert [segment.key for segment in chart.columns[0].segments] == ["delivered"]

    def test_columns_stay_inside_the_plot_area(self) -> None:
        chart = charts.stacked_column_chart(_days(45, sent=3, failed=1))

        for column in chart.columns:
            assert column.x >= chart.plot_left
            assert column.x + column.width <= chart.plot_right + 0.01

    def test_marks_stay_thin(self) -> None:
        """A column never fills its slot; the leftover is deliberate air."""
        chart = charts.stacked_column_chart(_days(2, sent=1))
        column = chart.columns[0]

        assert column.width <= charts.MAX_BAR_WIDTH
        assert column.width < column.hit_width

    def test_only_the_top_segment_is_rounded(self) -> None:
        """
        Rounding an interior segment would leave a notch above the one below.
        """
        chart = charts.stacked_column_chart(_days(1, read=5, delivered=5, failed=5))
        segments = {segment.key: segment.path for segment in chart.columns[0].segments}

        assert "Q" in segments["failed"]
        assert "Q" not in segments["read"]
        assert "Q" not in segments["delivered"]

    def test_the_table_view_carries_every_bucket_including_the_empty_ones(self) -> None:
        """
        The lightest step of the ramp is below 3:1 on the card, so the numbers
        have to be readable without seeing the colours.
        """
        chart = charts.stacked_column_chart(_days(1, delivered=4))
        column = chart.columns[0]

        assert len(column.table_cells) == len(chart.legend)
        assert sum(column.table_cells) == column.total

    def test_the_tooltip_payload_is_data_not_markup(self) -> None:
        import json

        chart = charts.stacked_column_chart(_days(1, delivered=4, failed=1))
        payload = json.loads(chart.columns[0].tooltip_json)

        assert payload["total"] == "5"
        assert [row["label"] for row in payload["rows"]] == ["Delivered", "Failed"]

    def test_axis_labels_thin_out_rather_than_colliding(self) -> None:
        chart = charts.stacked_column_chart(_days(60, sent=1))
        labelled = [column for column in chart.columns if column.show_label]

        assert len(labelled) <= charts.MAX_X_LABELS + 1
        assert chart.columns[-1].show_label is True

    def test_labels_neither_collide_nor_fall_off_the_edge(self) -> None:
        """
        The arithmetic that a human would otherwise have to catch by squinting.

        At 10px in the system sans a glyph is roughly 5.6px wide, so a date
        label is about 34px and the widest realistic axis figure about 40px.
        Both have to fit: the axis labels inside the left gutter, and the last
        date label inside the viewBox, which clips anything past its edge.
        """
        glyph = 5.6

        for count in (7, 14, 30, 62, 90, 366):
            for magnitude in (1, 40, 900, 25_000):
                chart = charts.stacked_column_chart(
                    _days(count, delivered=magnitude, failed=max(1, magnitude // 4))
                )
                labelled = [column for column in chart.columns if column.show_label]

                widest_tick = max(len(line.label) for line in chart.grid_lines) * glyph
                assert widest_tick <= chart.tick_label_x, (count, magnitude)

                label_width = max(len(column.short_label) for column in labelled) * glyph
                assert labelled[-1].centre_x + label_width / 2 <= chart.width
                assert labelled[0].centre_x - label_width / 2 >= 0

                gaps = [
                    later.centre_x - earlier.centre_x
                    for earlier, later in zip(labelled, labelled[1:], strict=False)
                ]
                assert all(gap >= label_width for gap in gaps), (count, magnitude)

    def test_no_mark_is_drawn_outside_the_plot_area(self) -> None:
        chart = charts.stacked_column_chart(_days(30, read=9, delivered=17, sent=4, failed=3))

        for column in chart.columns:
            for segment in column.segments:
                assert segment.y >= chart.plot_top - 0.01
                assert segment.y + segment.height <= chart.plot_bottom + 0.01

    def test_the_description_names_every_non_empty_series(self) -> None:
        chart = charts.stacked_column_chart(_days(1, read=2, failed=1))
        description = chart.columns[0].description

        assert "Read 2" in description
        assert "Failed 1" in description
        assert "Sent" not in description

    def test_the_stack_sits_on_the_baseline_and_grows_upwards(self) -> None:
        chart = charts.stacked_column_chart(_days(1, read=2, failed=2))
        segments = {segment.key: segment for segment in chart.columns[0].segments}

        # Read is the darkest step of the ramp and anchors the column; failures
        # ride on top, where a bad day shows along the skyline.
        assert segments["read"].y > segments["failed"].y
        bottom = segments["read"].y + segments["read"].height
        assert abs(bottom - chart.plot_bottom) <= charts.SEGMENT_GAP


class TestProportions:
    def test_percentages_are_of_the_campaign_total(self) -> None:
        parts = charts.proportions({"delivered": 3, "failed": 1})
        by_key = {part.key: part.percent for part in parts}

        assert by_key == {"delivered": 75.0, "failed": 25.0}

    def test_a_campaign_with_no_recipients_has_no_bar(self) -> None:
        assert charts.proportions({"delivered": 0, "failed": 0}) == []

    def test_in_flight_messages_are_reported_as_pending(self) -> None:
        from messaging.services import CampaignStats

        stats = CampaignStats(total=4, queued=2, sending=1, delivered=1)
        by_key = {part.key: part.value for part in charts.stats_proportions(stats)}

        assert by_key == {"pending": 3, "delivered": 1}
