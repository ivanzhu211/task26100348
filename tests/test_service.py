"""冲突生命周期、通知与校验的单元测试。"""
import unittest
from datetime import date

from campaign_conflicts import (
    CampaignNotFoundError,
    CampaignService,
    CampaignStatus,
    Recurrence,
    RecurrenceFreq,
    RegionWeek,
    ValidationError,
)

W1 = date(2026, 1, 5)   # 周一
W2 = date(2026, 1, 12)  # 下周一


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.svc = CampaignService(":memory:")

    def tearDown(self):
        self.svc.close()

    def create(self, name="c", manager="alice", regions={"R1"},
               start=W1, end=W1, status=CampaignStatus.SCHEDULED,
               recurrence=None):
        return self.svc.create_campaign(
            name, manager, regions, start, end, status, recurrence)

    # ---- 冲突检测 ----
    def test_same_week_same_region_conflicts_without_date_overlap(self):
        a = self.create(name="a", start=W1, end=W1)          # 周一
        b = self.create(name="b", manager="bob",
                        start=date(2026, 1, 11), end=date(2026, 1, 11))  # 同周周日
        confs = self.svc.get_current_conflicts(a)
        self.assertEqual(len(confs), 1)
        self.assertEqual(confs[0].campaign_b, b)
        self.assertEqual(confs[0].slots, frozenset({RegionWeek("R1", W1)}))

    def test_different_week_no_conflict(self):
        a = self.create(name="a", start=W1, end=W1)
        self.create(name="b", manager="bob", start=W2, end=W2)
        self.assertEqual(self.svc.get_current_conflicts(a), [])

    def test_different_region_no_conflict(self):
        a = self.create(name="a", regions={"R1"})
        self.create(name="b", manager="bob", regions={"R2"})
        self.assertEqual(self.svc.get_current_conflicts(a), [])

    def test_non_conflicting_status_ignored(self):
        a = self.create(name="a")
        for status in (CampaignStatus.DRAFT, CampaignStatus.COMPLETED,
                       CampaignStatus.CANCELLED):
            self.create(name="x", manager="bob", status=status)
        self.assertEqual(self.svc.get_current_conflicts(a), [])

    def test_recurrence_creates_conflict_in_later_week(self):
        a = self.create(name="a", start=W2, end=W2)
        b = self.create(name="b", manager="bob", start=W1, end=W1,
                        recurrence=Recurrence(RecurrenceFreq.WEEKLY, 2))
        confs = self.svc.get_current_conflicts(a)
        self.assertEqual(len(confs), 1)
        self.assertEqual(confs[0].slots, frozenset({RegionWeek("R1", W2)}))

    # ---- 冲突段生命周期 ----
    def test_slots_change_but_conflict_continues_same_episode(self):
        a = self.create(name="a", regions={"R1", "R2"})
        b = self.create(name="b", manager="bob", regions={"R1", "R2"})
        episode_id = self.svc.get_current_conflicts(a)[0].id
        self.svc.update_campaign(b, regions={"R2"})  # 共同区域周变少但没消失
        confs = self.svc.get_current_conflicts(a)
        self.assertEqual(len(confs), 1)
        self.assertEqual(confs[0].id, episode_id)  # 仍算同一段
        self.assertEqual(confs[0].slots, frozenset({RegionWeek("R2", W1)}))
        # 不重复通知
        self.assertEqual(len(self.svc.list_notifications()), 2)

    def test_conflict_ends_and_restarts_as_new_episode(self):
        a = self.create(name="a")
        b = self.create(name="b", manager="bob")
        self.svc.update_campaign(b, start_date=W2, end_date=W2)  # 结束
        self.assertEqual(self.svc.get_current_conflicts(a), [])
        self.svc.update_campaign(b, start_date=W1, end_date=W1)  # 再次撞上
        history = self.svc.get_conflict_history(a, b)
        self.assertEqual(len(history), 2)
        self.assertIsNotNone(history[0].ended_at)
        self.assertIsNone(history[1].ended_at)
        self.assertEqual(len(self.svc.list_notifications()), 4)

    def test_status_change_ends_conflict(self):
        a = self.create(name="a")
        b = self.create(name="b", manager="bob")
        self.svc.update_campaign(b, status=CampaignStatus.CANCELLED)
        self.assertEqual(self.svc.get_current_conflicts(a), [])
        history = self.svc.get_conflict_history(a, b)
        self.assertEqual(len(history), 1)
        self.assertIsNotNone(history[0].ended_at)

    # ---- 通知 ----
    def test_same_manager_gets_one_notification(self):
        a = self.create(name="a", manager="alice")
        self.create(name="b", manager="alice")
        notes = self.svc.list_notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].recipient, "alice")

    def test_both_managers_notified_once_per_episode(self):
        a = self.create(name="a", manager="alice")
        self.create(name="b", manager="bob")
        recipients = sorted(n.recipient for n in self.svc.list_notifications())
        self.assertEqual(recipients, ["alice", "bob"])

    # ---- 校验 ----
    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.create(start=W1, end=date(2025, 12, 31))  # 结束早于开始
        with self.assertRaises(ValidationError):
            self.create(start=W1, end=W1 + __import__("datetime").timedelta(days=365))  # 跨度 366 天
        with self.assertRaises(ValidationError):
            self.create(regions=set())
        with self.assertRaises(ValidationError):
            self.create(manager="")
        with self.assertRaises(ValidationError):
            self.create(recurrence=Recurrence(RecurrenceFreq.WEEKLY, 53))
        with self.assertRaises(ValidationError):
            self.create(recurrence=Recurrence(RecurrenceFreq.WEEKLY, 0))
        # 恰好 365 天可以
        self.create(start=W1, end=W1 + __import__("datetime").timedelta(days=364))

    def test_update_missing_campaign(self):
        with self.assertRaises(CampaignNotFoundError):
            self.svc.update_campaign(999, status=CampaignStatus.ACTIVE)

    def test_update_clear_recurrence(self):
        a = self.create(name="a", start=W2, end=W2)
        b = self.create(name="b", manager="bob", start=W1, end=W1,
                        recurrence=Recurrence(RecurrenceFreq.WEEKLY, 2))
        self.assertEqual(len(self.svc.get_current_conflicts(a)), 1)
        self.svc.update_campaign(b, recurrence=None)  # 显式清除重复
        self.assertEqual(self.svc.get_current_conflicts(a), [])
        self.assertIsNone(self.svc.get_campaign(b).recurrence)


if __name__ == "__main__":
    unittest.main()
