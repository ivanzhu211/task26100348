from datetime import date

from campaign_conflicts.models import (
    Campaign,
    CampaignStatus,
    RecurrenceFrequency,
    RecurrenceRule,
)
from campaign_conflicts.recurrence import campaign_cells, occurrence_ranges


def mk(start, end, rule=None, regions=("R1",)):
    return Campaign(
        id=1,
        owner_id="m",
        regions=frozenset(regions),
        start_date=start,
        end_date=end,
        status=CampaignStatus.SCHEDULED,
        recurrence=rule,
    )


def test_no_recurrence_single_occurrence():
    c = mk(date(2025, 5, 7), date(2025, 5, 9))
    assert occurrence_ranges(c) == [(date(2025, 5, 7), date(2025, 5, 9))]


def test_weekly_recurrence():
    c = mk(
        date(2025, 5, 7),
        date(2025, 5, 9),
        RecurrenceRule(RecurrenceFrequency.WEEKLY, 3),
    )
    assert occurrence_ranges(c) == [
        (date(2025, 5, 7), date(2025, 5, 9)),
        (date(2025, 5, 14), date(2025, 5, 16)),
        (date(2025, 5, 21), date(2025, 5, 23)),
    ]


def test_biweekly_recurrence_is_14_days():
    c = mk(
        date(2025, 5, 7),
        date(2025, 5, 7),
        RecurrenceRule(RecurrenceFrequency.BIWEEKLY, 3),
    )
    starts = [s for s, _ in occurrence_ranges(c)]
    assert starts == [date(2025, 5, 7), date(2025, 5, 21), date(2025, 6, 4)]


def test_monthly_recurrence_same_day_of_month():
    c = mk(
        date(2025, 1, 15),
        date(2025, 1, 16),
        RecurrenceRule(RecurrenceFrequency.MONTHLY, 3),
    )
    assert occurrence_ranges(c) == [
        (date(2025, 1, 15), date(2025, 1, 16)),
        (date(2025, 2, 15), date(2025, 2, 16)),
        (date(2025, 3, 15), date(2025, 3, 16)),
    ]


def test_monthly_recurrence_clamps_to_month_end_then_returns():
    c = mk(
        date(2025, 1, 31),
        date(2025, 1, 31),
        RecurrenceRule(RecurrenceFrequency.MONTHLY, 4),
    )
    starts = [s for s, _ in occurrence_ranges(c)]
    assert starts == [
        date(2025, 1, 31),
        date(2025, 2, 28),  # no Feb 31: last day of February
        date(2025, 3, 31),  # back to the 31st
        date(2025, 4, 30),  # no Apr 31
    ]


def test_monthly_recurrence_leap_year():
    c = mk(
        date(2024, 1, 31),
        date(2024, 1, 31),
        RecurrenceRule(RecurrenceFrequency.MONTHLY, 2),
    )
    starts = [s for s, _ in occurrence_ranges(c)]
    assert starts == [date(2024, 1, 31), date(2024, 2, 29)]


def test_occurrence_span_preserved_across_recurrence():
    c = mk(
        date(2025, 1, 30),
        date(2025, 2, 2),
        RecurrenceRule(RecurrenceFrequency.MONTHLY, 2),
    )
    ranges = occurrence_ranges(c)
    assert (ranges[0][1] - ranges[0][0]).days == 3
    assert (ranges[1][1] - ranges[1][0]).days == 3


def test_cells_cover_every_touched_week():
    # Wednesday to next Monday: touches two ISO weeks.
    c = mk(date(2025, 3, 5), date(2025, 3, 10))
    weeks = {cw.week_start for cw in campaign_cells(c)}
    assert weeks == {date(2025, 3, 3), date(2025, 3, 10)}


def test_cells_single_day_week_boundary():
    # A Sunday belongs to the week that started the previous Monday.
    c = mk(date(2025, 3, 9), date(2025, 3, 9))
    weeks = {cw.week_start for cw in campaign_cells(c)}
    assert weeks == {date(2025, 3, 3)}


def test_cells_include_all_regions():
    c = mk(date(2025, 3, 5), date(2025, 3, 5), regions=("R1", "R2"))
    regions = {cw.region for cw in campaign_cells(c)}
    assert regions == {"R1", "R2"}


def test_cells_union_across_occurrences():
    c = mk(
        date(2025, 3, 5),
        date(2025, 3, 5),
        RecurrenceRule(RecurrenceFrequency.WEEKLY, 2),
    )
    weeks = {cw.week_start for cw in campaign_cells(c)}
    assert weeks == {date(2025, 3, 3), date(2025, 3, 10)}
