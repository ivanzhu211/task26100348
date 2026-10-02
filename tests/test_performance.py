"""性能冒烟测试：活动总数增长时，单次写/查耗时不明显变长。

阈值刻意放宽（量级判断），只为防止出现全表扫描级别的退化。
注：大数据量通过单事务批量灌入，耗时的计时对象是经过服务层的单次操作。
"""
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from campaign_conflicts import CampaignService, CampaignStatus
from campaign_conflicts.models import Campaign
from campaign_conflicts.recurrence import campaign_region_weeks

BASE = date(2026, 1, 5)


class PerformanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "perf.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _populate(self, svc, n):
        """单事务批量灌入 n 个活动（含区域周索引），用于快速构造大数据量。"""
        store = svc._store()
        store.begin_immediate()
        for i in range(n):
            c = Campaign(
                id=0,
                name=f"c{i}",
                manager=f"m{i % 200}",
                regions=frozenset({f"R{i % 40}"}),
                start_date=BASE + timedelta(days=i % 365),
                end_date=BASE + timedelta(days=i % 365),
                status=CampaignStatus.SCHEDULED,
            )
            c.id = store.insert_campaign(c)
            store.replace_slots(c.id, campaign_region_weeks(c))
        store.commit()

    def test_write_and_query_stay_fast_as_data_grows(self):
        svc = CampaignService(self.db_path)
        self._populate(svc, 1000)

        def timed_create():
            t0 = time.perf_counter()
            svc.create_campaign("x", "mx", {"R1"}, BASE, BASE,
                                CampaignStatus.SCHEDULED)
            return time.perf_counter() - t0

        def timed_query():
            t0 = time.perf_counter()
            svc.get_current_conflicts(1)
            return time.perf_counter() - t0

        create_1k = timed_create()
        query_1k = timed_query()

        self._populate(svc, 19000)  # 总量到 2 万
        create_20k = timed_create()
        query_20k = timed_query()
        svc.close()

        # 数据量涨 20 倍，单次写/查耗时不允许出现数量级劣化
        self.assertLess(create_20k, max(create_1k * 10, 0.5))
        self.assertLess(query_20k, max(query_1k * 10, 0.2))


if __name__ == "__main__":
    unittest.main()
