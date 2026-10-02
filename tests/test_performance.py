"""Sanity checks that writes and conflict lookups stay fast as the number
of campaigns grows (loose bounds; not a micro-benchmark)."""

import random
import time
from datetime import date, timedelta

from campaign_conflicts import CampaignService, CampaignStatus

REGIONS = [f"R{i}" for i in range(40)]
BASE = date(2025, 1, 6)


def seed_campaigns(svc, count, rng):
    ids = []
    for i in range(count):
        start = BASE + timedelta(days=rng.randint(0, 300))
        ids.append(
            svc.create_campaign(
                owner_id=f"owner-{i % 200}",
                regions=set(rng.sample(REGIONS, rng.randint(1, 3))),
                start_date=start,
                end_date=start + timedelta(days=rng.randint(0, 14)),
                status=CampaignStatus.SCHEDULED,
            )
        )
    return ids


def timed(fn, repeats):
    start = time.perf_counter()
    for _ in range(repeats):
        fn()
    return (time.perf_counter() - start) / repeats


def test_write_and_read_latency_do_not_blow_up_with_scale(tmp_path):
    rng = random.Random(42)
    svc = CampaignService(str(tmp_path / "perf.db"))
    ids = seed_campaigns(svc, 1000, rng)
    day = BASE + timedelta(days=400)

    def one_create():
        svc.create_campaign(
            "perf-owner", {"R0"}, day, day + timedelta(days=3),
            CampaignStatus.SCHEDULED,
        )

    def one_update():
        svc.update_campaign(
            rng.choice(ids),
            start_date=day,
            end_date=day + timedelta(days=3),
        )

    def one_read():
        svc.current_conflicts(rng.choice(ids))

    create_1k = timed(one_create, 20)
    update_1k = timed(one_update, 20)
    read_1k = timed(one_read, 50)

    # Grow from 1k to 4k campaigns.
    ids += seed_campaigns(svc, 3000, rng)
    create_4k = timed(one_create, 20)
    update_4k = timed(one_update, 20)
    read_4k = timed(one_read, 50)

    # A single operation must stay interactive even on a slow disk, and
    # growing 1k -> 4k must not multiply the cost.
    assert create_4k < 1.0, f"create at 4k: {create_4k:.3f}s"
    assert update_4k < 1.0, f"update at 4k: {update_4k:.3f}s"
    assert read_4k < 0.5, f"read at 4k: {read_4k:.3f}s"
    assert create_4k < max(create_1k * 4, 0.05)
    assert update_4k < max(update_1k * 4, 0.05)
    assert read_4k < max(read_1k * 4, 0.05)
