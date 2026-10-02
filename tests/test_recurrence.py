"""重复展开与区域周计算的单元测试。"""
import unittest
from datetime import date, timedelta

from campaign_conflicts.models import (
    Campaign,
    CampaignStatus,
    Recurrence,
    RecurrenceFreq,
    RegionWeek,
)
from campaign_conflicts.recurrence import (
    campaign_region_weeks,
    occurrence_starts,
    week_start_of,
)


def make_campaign(**kw):
    defaults = dict(
        id=1,
        name="t",
        manager="m",
        regions=frozenset({"R1"}),
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 5),
        status=CampaignStatus.SCHEDULED,
        recurrence=None,
    )
    defaults.update(kw)
    return Campaign(**defaults)


class WeekStartTest(unittest.TestCase):
    def test_monday_to_sunday(self):
        monday = date(2026, 9, 28)
        for i in range(7):
            self.assertEqual(week_start_of(monday + timedelta(days=i)), monday)


class OccurrenceStartsTest(unittest.TestCase):
    def test_no_recurrence(self):
        c = make_campaign()
        self.assertEqual(occurrence_starts(c), [date(2026, 1, 5)])

    def test_weekly(self):
        c = make_campaign(recurrence=Recurrence(RecurrenceFreq.WEEKLY, 3))
        self.assertEqual(
            occurrence_starts(c),
            [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)],
        )

    def test_biweekly(self):
        c = make_campaign(recurrence=Recurrence(RecurrenceFreq.BIWEEKLY, 3))
        self.assertEqual(
            occurrence_starts(c),
            [date(2026, 1, 5), date(2026, 1, 19), date(2026, 2, 2)],
        )

    def test_monthly_same_day(self):
        c = make_campaign(
            start_date=date(2026, 1, 15),
            end_date=date(2026, 1, 15),
            recurrence=Recurrence(RecurrenceFreq.MONTHLY, 3),
        )
        self.assertEqual(
            occurrence_starts(c),
            [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)],
        )

    def test_monthly_clamps_to_last_day_then_returns(self):
        # 1月31日起：2月无31日落2月28日，3月回到31日
        c = make_campaign(
            start_date=date(2026, 1, 31),
            end_date=date(2026, 1, 31),
            recurrence=Recurrence(RecurrenceFreq.MONTHLY, 4),
        )
        self.assertEqual(
            occurrence_starts(c),
            [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)],
        )

    def test_monthly_leap_year(self):
        c = make_campaign(
            start_date=date(2027, 12, 31),
            end_date=date(2027, 12, 31),
            recurrence=Recurrence(RecurrenceFreq.MONTHLY, 3),
        )
        self.assertEqual(
            occurrence_starts(c),
            [date(2027, 12, 31), date(2028, 1, 31), date(2028, 2, 29)],
        )


class RegionWeeksTest(unittest.TestCase):
    def test_non_conflicting_status_gives_no_slots(self):
        for status in (CampaignStatus.DRAFT, CampaignStatus.COMPLETED,
                       CampaignStatus.CANCELLED):
            c = make_campaign(status=status)
            self.assertEqual(campaign_region_weeks(c), set())

    def test_conflicting_statuses_give_slots(self):
        for status in (CampaignStatus.SCHEDULED, CampaignStatus.ACTIVE,
                       CampaignStatus.PAUSED):
            c = make_campaign(status=status)
            self.assertEqual(
                campaign_region_weeks(c),
                {RegionWeek("R1", date(2026, 1, 5))},
            )

    def test_multi_region(self):
        c = make_campaign(regions=frozenset({"R1", "R2"}))
        self.assertEqual(
            campaign_region_weeks(c),
            {RegionWeek("R1", date(2026, 1, 5)), RegionWeek("R2", date(2026, 1, 5))},
        )

    def test_spanning_two_weeks(self):
        # 周三到下周二，占两周（哪怕只占其中一天）
        c = make_campaign(start_date=date(2026, 1, 7), end_date=date(2026, 1, 13))
        weeks = {rw.week_start for rw in campaign_region_weeks(c)}
        self.assertEqual(weeks, {date(2026, 1, 5), date(2026, 1, 12)})

    def test_duration_preserved_across_recurrences(self):
        c = make_campaign(
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 8),
            recurrence=Recurrence(RecurrenceFreq.WEEKLY, 2),
        )
        weeks = sorted(rw.week_start for rw in campaign_region_weeks(c))
        self.assertEqual(weeks, [date(2026, 1, 5), date(2026, 1, 12)])


if __name__ == "__main__":
    unittest.main()
