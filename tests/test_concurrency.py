"""多线程并发测试（验收场景二）。

多个线程同时往同一个区域周新建活动；全部结束后两两之间都有冲突，
每段冲突恰好通知一次。另覆盖并发新建与修改交错、以及事务原子性。
"""
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from campaign_conflicts import CampaignService, CampaignStatus, ValidationError

W1 = date(2026, 1, 5)  # 周一


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_concurrent_creates_same_region_week(self):
        n_threads, per_thread = 8, 5
        svc = CampaignService(self.db_path)
        self.addCleanup(svc.close)
        barrier = threading.Barrier(n_threads)
        errors = []
        created = []
        lock = threading.Lock()

        def worker(t: int):
            try:
                barrier.wait(timeout=30)
                for i in range(per_thread):
                    cid = svc.create_campaign(
                        name=f"t{t}-{i}",
                        manager=f"m{t}-{i}",
                        regions={"R1"},
                        start_date=W1,
                        end_date=W1,
                        status=CampaignStatus.SCHEDULED,
                    )
                    with lock:
                        created.append(cid)
            except Exception as e:  # pragma: no cover
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        self.assertEqual(errors, [])
        n = n_threads * per_thread
        self.assertEqual(len(created), n)

        # 两两之间都有冲突，且只有一段
        expected_pairs = n * (n - 1) // 2
        all_conflicts = set()
        for cid in created:
            for ep in svc.get_current_conflicts(cid):
                all_conflicts.add(tuple(sorted((ep.campaign_a, ep.campaign_b))))
        self.assertEqual(len(all_conflicts), expected_pairs)

        # 每段冲突恰好通知一次：双方负责人各一条
        notes = svc.list_notifications()
        self.assertEqual(len(notes), expected_pairs * 2)
        per_conflict = {}
        for note in notes:
            per_conflict.setdefault(note.conflict_id, set()).add(note.recipient)
        for pair in all_conflicts:
            history = svc.get_conflict_history(*pair)
            self.assertEqual(len(history), 1)  # 恰好一段
            ep = history[0]
            managers = {
                svc.get_campaign(ep.campaign_a).manager,
                svc.get_campaign(ep.campaign_b).manager,
            }
            self.assertEqual(per_conflict[ep.id], managers)
        svc.close()

    def test_concurrent_mixed_create_and_update(self):
        """并发新建与修改交错：结果必须等价于某个串行顺序。"""
        svc = CampaignService(self.db_path)
        self.addCleanup(svc.close)
        base_ids = [
            svc.create_campaign(f"base{i}", f"mb{i}", {"R1"}, W1, W1,
                                CampaignStatus.DRAFT)
            for i in range(6)
        ]
        barrier = threading.Barrier(6)
        errors = []

        def worker(t: int):
            try:
                barrier.wait(timeout=30)
                # 一半线程把草稿排进同一区域周，一半线程新建
                svc.update_campaign(base_ids[t], status=CampaignStatus.SCHEDULED)
                svc.create_campaign(f"new{t}", f"mn{t}", {"R1"}, W1, W1,
                                    CampaignStatus.SCHEDULED)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertEqual(errors, [])

        # 12 个活动全部落在同一区域周：两两一段冲突，通知数 = 段数 x 2
        n = 12
        total = 0
        for cid in range(1, n + 1):
            total += len(svc.get_current_conflicts(cid))
        self.assertEqual(total // 2, n * (n - 1) // 2)
        self.assertEqual(len(svc.list_notifications()), n * (n - 1))
        svc.close()

    def test_failed_update_rolls_back_atomically(self):
        """非法修改连同其冲突变化、通知一起回滚。"""
        svc = CampaignService(self.db_path)
        self.addCleanup(svc.close)
        a = svc.create_campaign("a", "alice", {"R1"}, W1, W1,
                                CampaignStatus.SCHEDULED)
        b = svc.create_campaign("b", "bob", {"R2"}, W1, W1,
                                CampaignStatus.SCHEDULED)
        before_conflicts = svc.get_current_conflicts(a)
        before_notes = len(svc.list_notifications())
        # 把 b 改到 R1（会产生冲突）但同时给一个非法日期，整体应回滚
        with self.assertRaises(ValidationError):
            svc.update_campaign(
                b, regions={"R1"},
                start_date=W1, end_date=date(2025, 1, 1),
            )
        self.assertEqual(svc.get_current_conflicts(a), before_conflicts)
        self.assertEqual(len(svc.list_notifications()), before_notes)
        self.assertEqual(svc.get_campaign(b).regions, frozenset({"R2"}))
        svc.close()


if __name__ == "__main__":
    unittest.main()
