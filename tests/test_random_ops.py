"""随机操作序列一致性测试（验收场景一）。

随机生成一长串新建/改日期/改区域/改重复/改状态操作，每步之后：
- 当前冲突（哪几对、各自撞在哪些区域周）与根据所有活动现状从头重算的结果完全一致；
- 发出的通知和冲突历史与参考模型一段一段对得上。
"""
import random
import unittest
from dataclasses import dataclass, field
from datetime import date, timedelta

from campaign_conflicts import (
    CampaignService,
    CampaignStatus,
    Recurrence,
    RecurrenceFreq,
)
from campaign_conflicts.models import Campaign
from campaign_conflicts.recurrence import campaign_region_weeks

BASE = date(2026, 1, 5)  # 周一
REGIONS = [f"R{i}" for i in range(8)]
MANAGERS = ["alice", "bob", "carol", "dave"]
STATUSES = list(CampaignStatus)
WINDOW_DAYS = 84  # 12 周窗口，制造大量撞车


def pair_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


@dataclass
class RefEpisode:
    recipients: frozenset  # 该段开始时双方负责人集合（去重）
    open: bool = True


@dataclass
class Reference:
    """参考模型：只根据活动现状全量重算，并推导冲突段与通知。"""

    campaigns: dict = field(default_factory=dict)          # id -> Campaign
    episodes: dict = field(default_factory=dict)           # pair -> [RefEpisode]

    def expected_pairs(self) -> dict:
        """从头算一遍：pair -> 共同区域周集合（仅保留非空）。"""
        slots = {cid: campaign_region_weeks(c) for cid, c in self.campaigns.items()}
        pairs = {}
        ids = sorted(slots)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                common = slots[a] & slots[b]
                if common:
                    pairs[(a, b)] = common
        return pairs

    def step(self) -> dict:
        """根据当前活动现状推进参考模型，返回期望的 pair -> slots。"""
        expected = self.expected_pairs()
        for pair in set(expected) | set(self.episodes):
            eps = self.episodes.setdefault(pair, [])
            open_ep = eps[-1] if eps and eps[-1].open else None
            if pair in expected:
                if open_ep is None:
                    managers = {
                        self.campaigns[pair[0]].manager,
                        self.campaigns[pair[1]].manager,
                    }
                    eps.append(RefEpisode(recipients=frozenset(managers)))
            elif open_ep is not None:
                open_ep.open = False
        return expected


class RandomOpsTest(unittest.TestCase):
    def run_scenario(self, seed: int, n_ops: int = 400):
        rng = random.Random(seed)
        svc = CampaignService(":memory:")
        ref = Reference()
        live_ids: list[int] = []

        def random_dates():
            start = BASE + timedelta(days=rng.randrange(WINDOW_DAYS))
            span = rng.choice([0, 0, 1, 3, 6, 13, 30])
            return start, start + timedelta(days=span)

        def random_recurrence():
            if rng.random() < 0.6:
                return None
            freq = rng.choice(list(RecurrenceFreq))
            return Recurrence(freq, rng.randint(1, 8))

        def random_regions():
            return set(rng.sample(REGIONS, rng.randint(1, 3)))

        for step in range(n_ops):
            if not live_ids or rng.random() < 0.35:
                start, end = random_dates()
                cid = svc.create_campaign(
                    name=f"c{step}",
                    manager=rng.choice(MANAGERS),
                    regions=random_regions(),
                    start_date=start,
                    end_date=end,
                    status=rng.choice(STATUSES),
                    recurrence=random_recurrence(),
                )
                live_ids.append(cid)
            else:
                cid = rng.choice(live_ids)
                kwargs = {}
                kind = rng.random()
                if kind < 0.3:
                    start, end = random_dates()
                    kwargs.update(start_date=start, end_date=end)
                elif kind < 0.5:
                    kwargs["regions"] = random_regions()
                elif kind < 0.7:
                    kwargs["status"] = rng.choice(STATUSES)
                elif kind < 0.85:
                    kwargs["recurrence"] = random_recurrence()  # 可能为 None（清除）
                else:
                    kwargs["manager"] = rng.choice(MANAGERS)
                svc.update_campaign(cid, **kwargs)

            # 同步参考模型中的活动现状
            ref.campaigns = {c.id: c for c in svc._store().list_campaigns()}
            expected = ref.step()

            # 1) 每个活动的当前冲突与从头重算一致
            for cid in live_ids:
                current = svc.get_current_conflicts(cid)
                got = {}
                for ep in current:
                    other = ep.campaign_b if ep.campaign_a == cid else ep.campaign_a
                    got[pair_key(cid, other)] = set(ep.slots)
                want = {p: s for p, s in expected.items() if cid in p}
                self.assertEqual(
                    got, want,
                    f"seed={seed} step={step} campaign={cid} 当前冲突不一致",
                )

            # 2) 冲突历史与参考模型一段一段对得上
            for pair, eps in ref.episodes.items():
                history = svc.get_conflict_history(*pair)
                self.assertEqual(
                    len(history), len(eps),
                    f"seed={seed} step={step} pair={pair} 段数不一致",
                )
                for ep_db, ep_ref in zip(history, eps):
                    self.assertEqual(ep_db.ended_at is None, ep_ref.open)
                if eps[-1].open:
                    self.assertEqual(set(history[-1].slots), expected[pair])

            # 3) 通知与冲突段对得上：每段恰好通知双方负责人（同人只一条）
            notes_by_conflict = {}
            for n in svc.list_notifications():
                notes_by_conflict.setdefault(n.conflict_id, set()).add(n.recipient)
            for pair, eps in ref.episodes.items():
                history = svc.get_conflict_history(*pair)
                for ep_db, ep_ref in zip(history, eps):
                    self.assertEqual(
                        notes_by_conflict.get(ep_db.id, set()),
                        set(ep_ref.recipients),
                        f"seed={seed} step={step} pair={pair} 通知不一致",
                    )
        svc.close()
        return ref

    def test_random_ops_multiple_seeds(self):
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                ref = self.run_scenario(seed)
                # 确保场景确实产生了足够多的冲突段，测试不是空转
                total_episodes = sum(len(e) for e in ref.episodes.values())
                self.assertGreater(total_episodes, 20)


if __name__ == "__main__":
    unittest.main()
