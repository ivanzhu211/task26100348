"""Public service API: campaign CRUD with conflict detection and notification."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import db
from .models import (
    Campaign,
    CampaignStatus,
    CONFLICTING_STATUSES,
    ConflictSegment,
    MAX_OCCURRENCES,
    MAX_SPAN_DAYS,
    Notification,
    RecurrenceRule,
    RegionWeek,
)
from .recurrence import campaign_cells


class ValidationError(ValueError):
    pass


#: Sentinel for "argument not provided" in update_campaign.
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate(
    owner_id: str,
    regions: Iterable[str],
    start_date: date,
    end_date: date,
    recurrence: Optional[RecurrenceRule],
) -> frozenset:
    if not owner_id:
        raise ValidationError("owner_id must be non-empty")
    region_set = frozenset(regions)
    if not region_set:
        raise ValidationError("regions must be non-empty")
    if any(not r for r in region_set):
        raise ValidationError("region names must be non-empty")
    if end_date < start_date:
        raise ValidationError("end_date must not be before start_date")
    if (end_date - start_date).days > MAX_SPAN_DAYS:
        raise ValidationError("campaign span must not exceed 365 days")
    if recurrence is not None and not (1 <= recurrence.occurrences <= MAX_OCCURRENCES):
        raise ValidationError("recurrence occurrences must be in 1..52")
    return region_set


def _row_to_campaign(row: sqlite3.Row, regions: frozenset) -> Campaign:
    recurrence = None
    if row["recurrence_frequency"] is not None:
        from .models import RecurrenceFrequency

        recurrence = RecurrenceRule(
            frequency=RecurrenceFrequency(row["recurrence_frequency"]),
            occurrences=row["recurrence_occurrences"],
        )
    return Campaign(
        id=row["id"],
        owner_id=row["owner_id"],
        regions=regions,
        start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]),
        status=CampaignStatus(row["status"]),
        recurrence=recurrence,
    )


class CampaignService:
    """Entry point used by the upper-layer campaign management service."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._local = threading.local()
        conn = db.connect(db_path)
        try:
            db.init_db(conn)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self._db_path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's connection (optional; connections are
        closed automatically when the service is garbage collected)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # write path
    # ------------------------------------------------------------------

    def create_campaign(
        self,
        owner_id: str,
        regions: Iterable[str],
        start_date: date,
        end_date: date,
        status: CampaignStatus,
        recurrence: Optional[RecurrenceRule] = None,
    ) -> int:
        """Create a campaign, return its id."""
        region_set = _validate(owner_id, regions, start_date, end_date, recurrence)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO campaigns "
                    "(owner_id, start_date, end_date, status, "
                    " recurrence_frequency, recurrence_occurrences) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        owner_id,
                        start_date.isoformat(),
                        end_date.isoformat(),
                        status.value,
                        recurrence.frequency.value if recurrence else None,
                        recurrence.occurrences if recurrence else None,
                    ),
                )
                campaign_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO campaign_regions (campaign_id, region) VALUES (?, ?)",
                    [(campaign_id, r) for r in sorted(region_set)],
                )
                campaign = Campaign(
                    id=campaign_id,
                    owner_id=owner_id,
                    regions=region_set,
                    start_date=start_date,
                    end_date=end_date,
                    status=status,
                    recurrence=recurrence,
                )
                self._refresh_campaign(conn, campaign)
                conn.execute("COMMIT")
                return campaign_id
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def update_campaign(
        self,
        campaign_id: int,
        *,
        owner_id: Optional[str] = None,
        regions: Optional[Iterable[str]] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        status: Optional[CampaignStatus] = None,
        recurrence=_UNSET,
    ) -> None:
        """Update fields of an existing campaign."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = self._load_campaign(conn, campaign_id)
                if current is None:
                    raise ValidationError(f"campaign {campaign_id} does not exist")
                new_recurrence = (
                    current.recurrence if recurrence is _UNSET else recurrence
                )
                updated = Campaign(
                    id=campaign_id,
                    owner_id=owner_id if owner_id is not None else current.owner_id,
                    regions=(
                        frozenset(regions) if regions is not None else current.regions
                    ),
                    start_date=(
                        start_date if start_date is not None else current.start_date
                    ),
                    end_date=end_date if end_date is not None else current.end_date,
                    status=status if status is not None else current.status,
                    recurrence=new_recurrence,
                )
                region_set = _validate(
                    updated.owner_id,
                    updated.regions,
                    updated.start_date,
                    updated.end_date,
                    updated.recurrence,
                )
                conn.execute(
                    "UPDATE campaigns SET owner_id = ?, start_date = ?, "
                    "end_date = ?, status = ?, recurrence_frequency = ?, "
                    "recurrence_occurrences = ? WHERE id = ?",
                    (
                        updated.owner_id,
                        updated.start_date.isoformat(),
                        updated.end_date.isoformat(),
                        updated.status.value,
                        (
                            updated.recurrence.frequency.value
                            if updated.recurrence
                            else None
                        ),
                        updated.recurrence.occurrences if updated.recurrence else None,
                        campaign_id,
                    ),
                )
                if regions is not None:
                    conn.execute(
                        "DELETE FROM campaign_regions WHERE campaign_id = ?",
                        (campaign_id,),
                    )
                    conn.executemany(
                        "INSERT INTO campaign_regions (campaign_id, region) "
                        "VALUES (?, ?)",
                        [(campaign_id, r) for r in sorted(region_set)],
                    )
                self._refresh_campaign(conn, updated)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # conflict maintenance (must be called inside a write transaction)
    # ------------------------------------------------------------------

    def _refresh_campaign(self, conn: sqlite3.Connection, campaign: Campaign) -> None:
        """Rebuild the campaign's cells, then reconcile its conflicts."""
        conn.execute(
            "DELETE FROM campaign_cells WHERE campaign_id = ?", (campaign.id,)
        )
        if campaign.status in CONFLICTING_STATUSES:
            cells = campaign_cells(campaign)
            conn.executemany(
                "INSERT INTO campaign_cells (campaign_id, region, week_start) "
                "VALUES (?, ?, ?)",
                [
                    (campaign.id, c.region, c.week_start.isoformat())
                    for c in cells
                ],
            )

        # Shared cells with every other campaign, grouped by campaign.
        shared: Dict[int, Set[Tuple[str, str]]] = {}
        rows = conn.execute(
            "SELECT other.campaign_id AS cid, other.region AS region, "
            "other.week_start AS week_start "
            "FROM campaign_cells other "
            "JOIN campaign_cells mine "
            "  ON mine.region = other.region "
            " AND mine.week_start = other.week_start "
            "WHERE mine.campaign_id = ? AND other.campaign_id != ?",
            (campaign.id, campaign.id),
        ).fetchall()
        for row in rows:
            shared.setdefault(row["cid"], set()).add(
                (row["region"], row["week_start"])
            )

        # Currently open segments involving this campaign.
        open_rows = conn.execute(
            "SELECT id, campaign_id_a, campaign_id_b FROM conflicts "
            "WHERE ended_at IS NULL AND (campaign_id_a = ? OR campaign_id_b = ?)",
            (campaign.id, campaign.id),
        ).fetchall()
        open_by_other: Dict[int, int] = {}
        for row in open_rows:
            other_id = (
                row["campaign_id_b"]
                if row["campaign_id_a"] == campaign.id
                else row["campaign_id_a"]
            )
            open_by_other[other_id] = row["id"]

        now = _now()

        # Close segments whose shared cells have all disappeared.
        for other_id, conflict_id in open_by_other.items():
            if other_id not in shared:
                conn.execute(
                    "UPDATE conflicts SET ended_at = ? WHERE id = ?",
                    (now, conflict_id),
                )
                conn.execute(
                    "DELETE FROM conflict_cells WHERE conflict_id = ?",
                    (conflict_id,),
                )

        # Open or refresh segments for campaigns sharing cells now.
        for other_id, cells in shared.items():
            conflict_id = open_by_other.get(other_id)
            if conflict_id is None:
                id_a, id_b = sorted((campaign.id, other_id))
                cur = conn.execute(
                    "INSERT INTO conflicts (campaign_id_a, campaign_id_b, "
                    "started_at) VALUES (?, ?, ?)",
                    (id_a, id_b, now),
                )
                conflict_id = cur.lastrowid
                self._notify_new_conflict(conn, conflict_id, id_a, id_b, now)
            else:
                conn.execute(
                    "DELETE FROM conflict_cells WHERE conflict_id = ?",
                    (conflict_id,),
                )
            conn.executemany(
                "INSERT INTO conflict_cells (conflict_id, region, week_start) "
                "VALUES (?, ?, ?)",
                [
                    (conflict_id, region, week_start)
                    for region, week_start in sorted(cells)
                ],
            )

    def _notify_new_conflict(
        self,
        conn: sqlite3.Connection,
        conflict_id: int,
        id_a: int,
        id_b: int,
        now: str,
    ) -> None:
        owners = dict(
            conn.execute(
                "SELECT id, owner_id FROM campaigns WHERE id IN (?, ?)",
                (id_a, id_b),
            ).fetchall()
        )
        owner_a, owner_b = owners[id_a], owners[id_b]
        recipients = [(owner_a, id_a, id_b)]
        if owner_b != owner_a:
            recipients.append((owner_b, id_b, id_a))
        conn.executemany(
            "INSERT INTO notifications "
            "(recipient_id, conflict_id, campaign_id, other_campaign_id, "
            " created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (recipient, conflict_id, cid, other_cid, now)
                for recipient, cid, other_cid in recipients
            ],
        )

    # ------------------------------------------------------------------
    # read path
    # ------------------------------------------------------------------

    def _load_campaign(
        self, conn: sqlite3.Connection, campaign_id: int
    ) -> Optional[Campaign]:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            return None
        regions = frozenset(
            r["region"]
            for r in conn.execute(
                "SELECT region FROM campaign_regions WHERE campaign_id = ?",
                (campaign_id,),
            )
        )
        return _row_to_campaign(row, regions)

    def get_campaign(self, campaign_id: int) -> Campaign:
        conn = self._connect()
        campaign = self._load_campaign(conn, campaign_id)
        if campaign is None:
            raise ValidationError(f"campaign {campaign_id} does not exist")
        return campaign

    def current_conflicts(self, campaign_id: int) -> List[dict]:
        """Open conflict segments involving the campaign, with current cells."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, campaign_id_a, campaign_id_b, started_at "
            "FROM conflicts "
            "WHERE ended_at IS NULL AND (campaign_id_a = ? OR campaign_id_b = ?) "
            "ORDER BY id",
            (campaign_id, campaign_id),
        ).fetchall()
        result = []
        for row in rows:
            other_id = (
                row["campaign_id_b"]
                if row["campaign_id_a"] == campaign_id
                else row["campaign_id_a"]
            )
            cells = [
                RegionWeek(
                    region=c["region"],
                    week_start=date.fromisoformat(c["week_start"]),
                )
                for c in conn.execute(
                    "SELECT region, week_start FROM conflict_cells "
                    "WHERE conflict_id = ? ORDER BY region, week_start",
                    (row["id"],),
                )
            ]
            result.append(
                {
                    "conflict_id": row["id"],
                    "other_campaign_id": other_id,
                    "started_at": row["started_at"],
                    "cells": cells,
                }
            )
        return result

    def conflict_history(
        self, campaign_id_a: int, campaign_id_b: int
    ) -> List[ConflictSegment]:
        """All segments (open and closed) for a pair, oldest first."""
        id_a, id_b = sorted((campaign_id_a, campaign_id_b))
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, campaign_id_a, campaign_id_b, started_at, ended_at "
            "FROM conflicts WHERE campaign_id_a = ? AND campaign_id_b = ? "
            "ORDER BY id",
            (id_a, id_b),
        ).fetchall()
        return [
            ConflictSegment(
                id=r["id"],
                campaign_id_a=r["campaign_id_a"],
                campaign_id_b=r["campaign_id_b"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
            )
            for r in rows
        ]

    def notifications_for(self, recipient_id: str) -> List[Notification]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, recipient_id, conflict_id, campaign_id, "
            "other_campaign_id, created_at FROM notifications "
            "WHERE recipient_id = ? ORDER BY id",
            (recipient_id,),
        ).fetchall()
        return [
            Notification(
                id=r["id"],
                recipient_id=r["recipient_id"],
                conflict_id=r["conflict_id"],
                campaign_id=r["campaign_id"],
                other_campaign_id=r["other_campaign_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
