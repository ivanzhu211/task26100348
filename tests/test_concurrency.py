"""Concurrency tests: interleaved submissions must behave as if serialized."""

import random
import threading
from datetime import date, timedelta

from campaign_conflicts import CampaignService, CampaignStatus
from campaign_conflicts.models import CONFLICTING_STATUSES
from campaign_conflicts.recurrence import campaign_cells

WEEK_MONDAY = date(2025, 6, 2)  # a Monday


def expected_conflicts_from_scratch(campaigns):
    """pair -> shared cells, recomputed from all current campaigns."""
    cells = {}
    for cid, c in campaigns.items():
        if c.status in CONFLICTING_STATUSES:
            cells[cid] = {(cw.region, cw.week_start) for cw in campaign_cells(c)}
    expected = {}
    ids = sorted(cells)
    for i, id_a in enumerate(ids):
        for id_b in ids[i + 1:]:
            shared = cells[id_a] & cells[id_b]
            if shared:
                expected[(id_a, id_b)] = shared
    return expected


def test_threads_create_into_same_region_week(tmp_path):
    svc = CampaignService(str(tmp_path / "conc.db"))
    n_threads, per_thread = 8, 5
    total = n_threads * per_thread
    barrier = threading.Barrier(n_threads)
    errors = []
    created = []
    created_lock = threading.Lock()

    def worker(worker_index):
        try:
            barrier.wait(timeout=30)
            for j in range(per_thread):
                owner = f"owner-{worker_index}-{j}"
                cid = svc.create_campaign(
                    owner_id=owner,
                    regions={"SHARED"},
                    start_date=WEEK_MONDAY + timedelta(days=j % 7),
                    end_date=WEEK_MONDAY + timedelta(days=j % 7),
                    status=CampaignStatus.SCHEDULED,
                )
                with created_lock:
                    created.append(cid)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(w,)) for w in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors
    assert len(created) == total

    # Every pair of campaigns conflicts: total choose 2 open segments.
    expected_pairs = total * (total - 1) // 2
    open_segments = 0
    for cid in created:
        conflicts = svc.current_conflicts(cid)
        assert len(conflicts) == total - 1
        for entry in conflicts:
            assert {(c.region, c.week_start) for c in entry["cells"]} == {
                ("SHARED", WEEK_MONDAY)
            }
        open_segments += len(conflicts)
    assert open_segments // 2 == expected_pairs

    # Each pair has exactly one segment, still open.
    for i, id_a in enumerate(sorted(created)):
        for id_b in sorted(created)[i + 1:]:
            history = svc.conflict_history(id_a, id_b)
            assert len(history) == 1
            assert history[0].ended_at is None

    # Each owner was notified exactly once per other campaign: no segment
    # notified twice, none missed.
    for w in range(n_threads):
        for j in range(per_thread):
            notifications = svc.notifications_for(f"owner-{w}-{j}")
            assert len(notifications) == total - 1


def test_threads_mixed_writes_end_state_matches_oracle(tmp_path):
    """Concurrent creates/updates; the final state must equal some
    serialization, which the from-scratch oracle validates."""
    svc = CampaignService(str(tmp_path / "mixed.db"))
    base = date(2025, 6, 2)

    ids = [
        svc.create_campaign(
            owner_id=f"owner-{i}",
            regions={f"R{i % 3}"},
            start_date=base,
            end_date=base + timedelta(days=2),
            status=CampaignStatus.DRAFT,
        )
        for i in range(12)
    ]

    barrier = threading.Barrier(6)
    errors = []

    def worker(worker_index):
        local = random.Random(100 + worker_index)
        try:
            barrier.wait(timeout=30)
            for _ in range(30):
                cid = local.choice(ids)
                day = base + timedelta(days=local.randint(0, 13))
                svc.update_campaign(
                    cid,
                    start_date=day,
                    end_date=day + timedelta(days=local.randint(0, 3)),
                    regions={f"R{local.randint(0, 2)}"},
                    status=local.choice(
                        [
                            CampaignStatus.SCHEDULED,
                            CampaignStatus.ACTIVE,
                            CampaignStatus.PAUSED,
                            CampaignStatus.CANCELLED,
                        ]
                    ),
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(w,)) for w in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors

    campaigns = {cid: svc.get_campaign(cid) for cid in ids}
    expected = expected_conflicts_from_scratch(campaigns)

    actual = {}
    for cid in ids:
        for entry in svc.current_conflicts(cid):
            pair = tuple(sorted((cid, entry["other_campaign_id"])))
            actual[pair] = {(c.region, c.week_start) for c in entry["cells"]}
    assert actual == expected

    # Exactly one open segment per currently-conflicting pair.
    for pair in expected:
        history = svc.conflict_history(*pair)
        assert history[-1].ended_at is None
        assert sum(1 for s in history if s.ended_at is None) == 1
