"""Property test: after every random operation, the conflicts reported by the
service must match a from-scratch recomputation over all current campaigns,
and notifications/history must line up segment by segment."""

import random
from datetime import date, timedelta

import pytest

from campaign_conflicts import (
    CampaignService,
    CampaignStatus,
    RecurrenceFrequency,
    RecurrenceRule,
)
from campaign_conflicts.models import CONFLICTING_STATUSES, Campaign
from campaign_conflicts.recurrence import campaign_cells

OWNERS = ["alice", "bob", "carol"]
REGIONS = [f"R{i}" for i in range(6)]
STATUSES = list(CampaignStatus)
BASE = date(2025, 1, 6)  # a Monday


def random_recurrence(rng):
    if rng.random() < 0.5:
        return None
    return RecurrenceRule(
        frequency=rng.choice(list(RecurrenceFrequency)),
        occurrences=rng.randint(1, 8),
    )


def random_dates(rng):
    start = BASE + timedelta(days=rng.randint(0, 45))
    end = start + timedelta(days=rng.randint(0, 10))
    return start, end


def random_regions(rng):
    return set(rng.sample(REGIONS, rng.randint(1, 3)))


class Model:
    """Mirror of every campaign's current state, used as the oracle."""

    def __init__(self):
        self.campaigns = {}  # id -> Campaign
        self.pair_active = set()  # pairs currently conflicting
        self.pair_openings = {}  # pair -> number of segments opened
        self.owner_notifications = {o: 0 for o in OWNERS}

    def expected_conflicts(self):
        """pair -> set of shared (region, week_start) cells, from scratch."""
        cells = {}
        for cid, c in self.campaigns.items():
            if c.status in CONFLICTING_STATUSES:
                cells[cid] = {
                    (cw.region, cw.week_start) for cw in campaign_cells(c)
                }
        expected = {}
        ids = sorted(cells)
        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1:]:
                shared = cells[id_a] & cells[id_b]
                if shared:
                    expected[(id_a, id_b)] = shared
        return expected

    def advance(self, expected):
        """Fold the new expected conflict set into notifications/history."""
        now_active = set(expected)
        for pair in now_active - self.pair_active:
            self.pair_openings[pair] = self.pair_openings.get(pair, 0) + 1
            owners = {self.campaigns[cid].owner_id for cid in pair}
            for owner in owners:
                self.owner_notifications[owner] += 1
        self.pair_active = now_active


def service_conflicts(svc, model):
    """Aggregate the service's current_conflicts into pair -> cells."""
    result = {}
    for cid in model.campaigns:
        for entry in svc.current_conflicts(cid):
            pair = tuple(sorted((cid, entry["other_campaign_id"])))
            cells = frozenset(
                (c.region, c.week_start) for c in entry["cells"]
            )
            assert pair not in result or result[pair] == cells
            result[pair] = cells
    return result


def check_history(svc, model):
    for (id_a, id_b), openings in model.pair_openings.items():
        history = svc.conflict_history(id_a, id_b)
        assert len(history) == openings, (id_a, id_b)
        for segment in history[:-1]:
            assert segment.ended_at is not None
        is_open = (id_a, id_b) in model.pair_active
        if is_open:
            assert history[-1].ended_at is None
        else:
            assert history[-1].ended_at is not None


def check_notifications(svc, model):
    for owner, expected_count in model.owner_notifications.items():
        notifications = svc.notifications_for(owner)
        assert len(notifications) == expected_count, owner
        # A segment never notifies the same recipient twice.
        conflict_ids = [n.conflict_id for n in notifications]
        assert len(conflict_ids) == len(set(conflict_ids))


def run_random_sequence(db_path, seed, steps=300):
    rng = random.Random(seed)
    svc = CampaignService(str(db_path))
    model = Model()

    for step in range(steps):
        op = rng.random()
        if op < 0.3 or not model.campaigns:
            start, end = random_dates(rng)
            cid = svc.create_campaign(
                owner_id=rng.choice(OWNERS),
                regions=random_regions(rng),
                start_date=start,
                end_date=end,
                status=rng.choice(STATUSES),
                recurrence=random_recurrence(rng),
            )
            model.campaigns[cid] = svc.get_campaign(cid)
        else:
            cid = rng.choice(sorted(model.campaigns))
            field = rng.choice(
                ["dates", "regions", "recurrence", "status", "owner"]
            )
            kwargs = {}
            if field == "dates":
                start, end = random_dates(rng)
                kwargs = {"start_date": start, "end_date": end}
            elif field == "regions":
                kwargs = {"regions": random_regions(rng)}
            elif field == "recurrence":
                kwargs = {"recurrence": random_recurrence(rng)}
            elif field == "status":
                kwargs = {"status": rng.choice(STATUSES)}
            else:
                kwargs = {"owner_id": rng.choice(OWNERS)}
            svc.update_campaign(cid, **kwargs)
            model.campaigns[cid] = svc.get_campaign(cid)

        expected = model.expected_conflicts()
        model.advance(expected)

        actual = service_conflicts(svc, model)
        assert actual == expected, f"seed={seed} step={step}"

        if step % 25 == 0 or step == steps - 1:
            check_history(svc, model)
            check_notifications(svc, model)

    check_history(svc, model)
    check_notifications(svc, model)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_operations_match_from_scratch_oracle(tmp_path, seed):
    run_random_sequence(tmp_path / f"random_{seed}.db", seed)
