"""Expansion of campaign schedules into occupied (region, week) cells."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import List, Set, Tuple

from .models import Campaign, RecurrenceFrequency, RegionWeek


def _add_months_clamped(day: date, months: int) -> date:
    """Same day-of-month `months` later; clamps to the month's last day."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def occurrence_ranges(campaign: Campaign) -> List[Tuple[date, date]]:
    """Inclusive [start, end] date ranges of every occurrence."""
    rule = campaign.recurrence
    count = rule.occurrences if rule is not None else 1
    span = campaign.end_date - campaign.start_date
    ranges: List[Tuple[date, date]] = []
    start = campaign.start_date
    for i in range(count):
        if i > 0:
            if rule.frequency is RecurrenceFrequency.WEEKLY:
                start = campaign.start_date + timedelta(weeks=i)
            elif rule.frequency is RecurrenceFrequency.BIWEEKLY:
                start = campaign.start_date + timedelta(weeks=2 * i)
            else:  # MONTHLY
                start = _add_months_clamped(campaign.start_date, i)
        ranges.append((start, start + span))
    return ranges


def _weeks_touched(start: date, end: date) -> List[date]:
    """Mondays of every week that contains at least one day of [start, end]."""
    first_monday = start - timedelta(days=start.weekday())
    last_monday = end - timedelta(days=end.weekday())
    mondays = []
    monday = first_monday
    while monday <= last_monday:
        mondays.append(monday)
        monday += timedelta(weeks=1)
    return mondays


def campaign_cells(campaign: Campaign) -> Set[RegionWeek]:
    """All (region, week) cells touched by any occurrence of the campaign."""
    cells: Set[RegionWeek] = set()
    for occ_start, occ_end in occurrence_ranges(campaign):
        for monday in _weeks_touched(occ_start, occ_end):
            for region in campaign.regions:
                cells.add(RegionWeek(region=region, week_start=monday))
    return cells
