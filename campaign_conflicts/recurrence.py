"""重复规则展开与区域周计算。"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from .models import CONFLICTING_STATUSES, Campaign, RecurrenceFreq, RegionWeek


def week_start_of(day: date) -> date:
    """返回 day 所在周的周一。"""
    return day - timedelta(days=day.weekday())


def _add_months_same_day(day: date, months: int) -> date:
    """day 加 months 个月，落在同一日号；当月无此日则落当月最后一天。"""
    month_index = (day.year * 12 + (day.month - 1)) + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def occurrence_starts(campaign: Campaign) -> list[date]:
    """展开活动的每一次开始日期（含首次）。"""
    rec = campaign.recurrence
    if rec is None:
        return [campaign.start_date]
    starts = [campaign.start_date]
    current = campaign.start_date
    for _ in range(rec.count - 1):
        if rec.freq is RecurrenceFreq.WEEKLY:
            current = current + timedelta(days=7)
        elif rec.freq is RecurrenceFreq.BIWEEKLY:
            current = current + timedelta(days=14)
        elif rec.freq is RecurrenceFreq.MONTHLY:
            # 始终基于首次开始日的日号推算，保证“后面的月份照常回到原日号”
            current = _add_months_same_day(
                campaign.start_date, len(starts)
            )
        else:  # pragma: no cover - 防御未知枚举
            raise ValueError(f"unknown recurrence freq: {rec.freq}")
        starts.append(current)
    return starts


def campaign_region_weeks(campaign: Campaign) -> set[RegionWeek]:
    """计算活动占用的全部区域周。不参与冲突判断的状态返回空集。"""
    if campaign.status not in CONFLICTING_STATUSES:
        return set()
    duration = campaign.end_date - campaign.start_date
    slots: set[RegionWeek] = set()
    for occ_start in occurrence_starts(campaign):
        occ_end = occ_start + duration
        week = week_start_of(occ_start)
        last_week = week_start_of(occ_end)
        while week <= last_week:
            for region in campaign.regions:
                slots.add(RegionWeek(region=region, week_start=week))
            week += timedelta(days=7)
    return slots
