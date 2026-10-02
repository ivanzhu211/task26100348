import pytest
from datetime import date

from campaign_conflicts import (
    CampaignService,
    CampaignStatus,
    RecurrenceFrequency,
    RecurrenceRule,
    ValidationError,
)


@pytest.fixture()
def svc(tmp_path):
    return CampaignService(str(tmp_path / "test.db"))


def make(svc, owner="alice", regions=("R1",), start=date(2025, 3, 3),
         end=date(2025, 3, 5), status=CampaignStatus.SCHEDULED, recurrence=None):
    return svc.create_campaign(owner, regions, start, end, status, recurrence)


def test_conflict_same_week_overlapping_region_no_date_overlap(svc):
    a = make(svc, regions=("R1",), start=date(2025, 3, 3), end=date(2025, 3, 4))
    b = make(svc, owner="bob", regions=("R1",),
             start=date(2025, 3, 8), end=date(2025, 3, 9))
    conflicts = svc.current_conflicts(a)
    assert len(conflicts) == 1
    assert conflicts[0]["other_campaign_id"] == b
    assert {(c.region, c.week_start) for c in conflicts[0]["cells"]} == {
        ("R1", date(2025, 3, 3))
    }


def test_no_conflict_different_weeks(svc):
    a = make(svc, start=date(2025, 3, 3), end=date(2025, 3, 5))
    make(svc, owner="bob", start=date(2025, 3, 10), end=date(2025, 3, 12))
    assert svc.current_conflicts(a) == []


def test_no_conflict_disjoint_regions(svc):
    a = make(svc, regions=("R1",))
    make(svc, owner="bob", regions=("R2",))
    assert svc.current_conflicts(a) == []


def test_no_conflict_for_non_conflicting_statuses(svc):
    for status in (CampaignStatus.DRAFT, CampaignStatus.COMPLETED,
                   CampaignStatus.CANCELLED):
        a = make(svc, status=status)
        assert svc.current_conflicts(a) == []


def test_conflicting_statuses_participate(svc):
    for status in (CampaignStatus.SCHEDULED, CampaignStatus.ACTIVE,
                   CampaignStatus.PAUSED):
        a = make(svc, status=status)
        b = make(svc, owner="bob")
        others = {c["other_campaign_id"] for c in svc.current_conflicts(a)}
        assert b in others


def test_same_campaign_recurrences_do_not_self_conflict(svc):
    a = make(svc, recurrence=RecurrenceRule(RecurrenceFrequency.WEEKLY, 10))
    assert svc.current_conflicts(a) == []


def test_notifications_sent_to_both_owners_once(svc):
    a = make(svc, owner="alice")
    b = make(svc, owner="bob")
    assert len(svc.notifications_for("alice")) == 1
    assert len(svc.notifications_for("bob")) == 1
    n = svc.notifications_for("alice")[0]
    assert n.campaign_id == a and n.other_campaign_id == b


def test_same_owner_gets_single_notification(svc):
    make(svc, owner="alice")
    make(svc, owner="alice")
    assert len(svc.notifications_for("alice")) == 1


def test_segment_ends_when_cells_gone_and_reopens_as_new_segment(svc):
    a = make(svc, owner="alice")
    b = make(svc, owner="bob")
    svc.update_campaign(b, start_date=date(2025, 4, 7), end_date=date(2025, 4, 9))
    assert svc.current_conflicts(a) == []
    history = svc.conflict_history(a, b)
    assert len(history) == 1 and history[0].ended_at is not None

    svc.update_campaign(b, start_date=date(2025, 3, 6), end_date=date(2025, 3, 8))
    history = svc.conflict_history(a, b)
    assert len(history) == 2
    assert history[1].ended_at is None
    assert len(svc.notifications_for("alice")) == 2
    assert len(svc.notifications_for("bob")) == 2


def test_segment_survives_cell_change_while_still_conflicting(svc):
    a = make(svc, owner="alice", regions=("R1", "R2"))
    b = make(svc, owner="bob", regions=("R1", "R2"))
    # Change which cells collide, but keep at least one shared cell.
    svc.update_campaign(b, regions=("R2",))
    history = svc.conflict_history(a, b)
    assert len(history) == 1 and history[0].ended_at is None
    cells = svc.current_conflicts(a)[0]["cells"]
    assert {(c.region, c.week_start) for c in cells} == {
        ("R2", date(2025, 3, 3))
    }
    # No new notification for the continuing segment.
    assert len(svc.notifications_for("alice")) == 1


def test_status_change_closes_and_reopens_conflict(svc):
    a = make(svc, owner="alice")
    b = make(svc, owner="bob")
    svc.update_campaign(b, status=CampaignStatus.PAUSED)
    assert len(svc.current_conflicts(a)) == 1  # paused still conflicts
    svc.update_campaign(b, status=CampaignStatus.CANCELLED)
    assert svc.current_conflicts(a) == []
    svc.update_campaign(b, status=CampaignStatus.SCHEDULED)
    assert len(svc.current_conflicts(a)) == 1
    assert len(svc.conflict_history(a, b)) == 2


def test_recurrence_change_updates_conflict_cells(svc):
    a = make(svc, owner="alice", start=date(2025, 3, 3), end=date(2025, 3, 3))
    b = make(svc, owner="bob", start=date(2025, 3, 17), end=date(2025, 3, 17))
    assert svc.current_conflicts(a) == []
    svc.update_campaign(
        b, recurrence=RecurrenceRule(RecurrenceFrequency.WEEKLY, 3)
    )
    # b now also runs on Mar 31 and Apr 14; still no shared week with a.
    assert svc.current_conflicts(a) == []
    svc.update_campaign(
        a, recurrence=RecurrenceRule(RecurrenceFrequency.BIWEEKLY, 2)
    )
    # a now runs Mar 3 and Mar 17 -> collides with b's Mar 17 occurrence.
    conflicts = svc.current_conflicts(a)
    assert len(conflicts) == 1
    assert {(c.region, c.week_start) for c in conflicts[0]["cells"]} == {
        ("R1", date(2025, 3, 17))
    }


def test_validation_rejects_bad_input(svc):
    with pytest.raises(ValidationError):
        make(svc, end=date(2025, 3, 1))  # end before start
    with pytest.raises(ValidationError):
        make(svc, end=date(2026, 3, 4))  # span > 365 days
    with pytest.raises(ValidationError):
        make(svc, regions=())
    with pytest.raises(ValidationError):
        make(svc, recurrence=RecurrenceRule(RecurrenceFrequency.WEEKLY, 53))
    with pytest.raises(ValidationError):
        make(svc, owner="")


def test_update_unknown_campaign_raises(svc):
    with pytest.raises(ValidationError):
        svc.update_campaign(999, status=CampaignStatus.ACTIVE)


def test_atomicity_failed_update_leaves_no_trace(svc):
    a = make(svc, owner="alice")
    b = make(svc, owner="bob")
    with pytest.raises(ValidationError):
        svc.update_campaign(b, end_date=date(2020, 1, 1))  # invalid
    # State is exactly as before the failed update.
    assert svc.get_campaign(b).end_date == date(2025, 3, 5)
    assert len(svc.current_conflicts(a)) == 1
    assert len(svc.conflict_history(a, b)) == 1
    assert len(svc.notifications_for("alice")) == 1


def test_history_is_per_pair(svc):
    a = make(svc, owner="alice")
    b = make(svc, owner="bob")
    c = make(svc, owner="carol", start=date(2025, 5, 5), end=date(2025, 5, 7))
    assert svc.conflict_history(a, c) == []
    assert len(svc.conflict_history(a, b)) == 1
    assert len(svc.conflict_history(b, a)) == 1  # order-independent
