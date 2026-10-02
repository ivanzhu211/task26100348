"""SQLite 持久化层：活动、区域周占用、冲突段、通知。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from .models import (
    Campaign,
    CampaignStatus,
    ConflictEpisode,
    Notification,
    Recurrence,
    RecurrenceFreq,
    RegionWeek,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    manager TEXT NOT NULL,
    regions TEXT NOT NULL,          -- JSON 数组
    start_date TEXT NOT NULL,       -- ISO 日期
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,
    recurrence_freq TEXT,           -- NULL 表示不重复
    recurrence_count INTEGER
);

-- 每个活动当前占用的区域周（冗余索引，随活动写事务一起维护）
CREATE TABLE IF NOT EXISTS campaign_slots (
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    region TEXT NOT NULL,
    week_start TEXT NOT NULL,
    PRIMARY KEY (campaign_id, region, week_start)
);
CREATE INDEX IF NOT EXISTS idx_slots_region_week
    ON campaign_slots (region, week_start);

-- 冲突段：一对活动之间的一段持续冲突
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_a INTEGER NOT NULL,
    campaign_b INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,                  -- NULL 表示仍在持续
    UNIQUE (campaign_a, campaign_b, started_at)
);
CREATE INDEX IF NOT EXISTS idx_conflicts_pair
    ON conflicts (campaign_a, campaign_b);
CREATE INDEX IF NOT EXISTS idx_conflicts_active
    ON conflicts (campaign_a) WHERE ended_at IS NULL;

-- 进行中的冲突段当前撞在哪些区域周
CREATE TABLE IF NOT EXISTS conflict_slots (
    conflict_id INTEGER NOT NULL REFERENCES conflicts(id) ON DELETE CASCADE,
    region TEXT NOT NULL,
    week_start TEXT NOT NULL,
    PRIMARY KEY (conflict_id, region, week_start)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_id INTEGER NOT NULL REFERENCES conflicts(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Store:
    """对单个 SQLite 数据库的存取。每个实例绑定一个连接，不可跨线程共享。"""

    def __init__(self, db_path: str):
        # check_same_thread=False：每个 Store 只被其所属线程使用，
        # 但允许主线程在工作线程结束后统一 close（见 CampaignService.close）
        self._conn = sqlite3.connect(db_path, isolation_level=None,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        # WAL + NORMAL：写事务不阻塞读，提交无需每次 fsync，崩溃仍不丢已提交事务
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 事务 ----
    def begin_immediate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    # ---- 活动 ----
    @staticmethod
    def _row_to_campaign(row: sqlite3.Row | tuple) -> Campaign:
        (cid, name, manager, regions, start_date, end_date, status,
         rec_freq, rec_count) = row
        recurrence = None
        if rec_freq is not None:
            recurrence = Recurrence(RecurrenceFreq(rec_freq), rec_count)
        return Campaign(
            id=cid,
            name=name,
            manager=manager,
            regions=frozenset(json.loads(regions)),
            start_date=date.fromisoformat(start_date),
            end_date=date.fromisoformat(end_date),
            status=CampaignStatus(status),
            recurrence=recurrence,
        )

    def insert_campaign(self, campaign: Campaign) -> int:
        rec = campaign.recurrence
        cur = self._conn.execute(
            "INSERT INTO campaigns (name, manager, regions, start_date, end_date,"
            " status, recurrence_freq, recurrence_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                campaign.name,
                campaign.manager,
                json.dumps(sorted(campaign.regions)),
                campaign.start_date.isoformat(),
                campaign.end_date.isoformat(),
                campaign.status.value,
                rec.freq.value if rec else None,
                rec.count if rec else None,
            ),
        )
        return cur.lastrowid

    def update_campaign(self, campaign: Campaign) -> None:
        rec = campaign.recurrence
        self._conn.execute(
            "UPDATE campaigns SET name=?, manager=?, regions=?, start_date=?,"
            " end_date=?, status=?, recurrence_freq=?, recurrence_count=?"
            " WHERE id=?",
            (
                campaign.name,
                campaign.manager,
                json.dumps(sorted(campaign.regions)),
                campaign.start_date.isoformat(),
                campaign.end_date.isoformat(),
                campaign.status.value,
                rec.freq.value if rec else None,
                rec.count if rec else None,
                campaign.id,
            ),
        )

    def get_campaign(self, campaign_id: int) -> Campaign | None:
        row = self._conn.execute(
            "SELECT id, name, manager, regions, start_date, end_date, status,"
            " recurrence_freq, recurrence_count FROM campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()
        return self._row_to_campaign(row) if row else None

    def list_campaigns(self) -> list[Campaign]:
        rows = self._conn.execute(
            "SELECT id, name, manager, regions, start_date, end_date, status,"
            " recurrence_freq, recurrence_count FROM campaigns"
        ).fetchall()
        return [self._row_to_campaign(r) for r in rows]

    # ---- 区域周占用 ----
    def replace_slots(self, campaign_id: int, slots: set[RegionWeek]) -> None:
        self._conn.execute(
            "DELETE FROM campaign_slots WHERE campaign_id=?", (campaign_id,)
        )
        self._conn.executemany(
            "INSERT INTO campaign_slots (campaign_id, region, week_start)"
            " VALUES (?, ?, ?)",
            [(campaign_id, s.region, s.week_start.isoformat()) for s in slots],
        )

    def get_slots(self, campaign_id: int) -> set[RegionWeek]:
        rows = self._conn.execute(
            "SELECT region, week_start FROM campaign_slots WHERE campaign_id=?",
            (campaign_id,),
        ).fetchall()
        return {
            RegionWeek(region=r, week_start=date.fromisoformat(w)) for r, w in rows
        }

    def campaigns_sharing_slots(
        self, slots: set[RegionWeek], exclude: int
    ) -> dict[int, set[RegionWeek]]:
        """返回与给定区域周集合有交集的其他活动及其共享的区域周。"""
        result: dict[int, set[RegionWeek]] = {}
        if not slots:
            return result
        # 按 (region, week_start) 逐个查，命中 idx_slots_region_week 索引
        for s in slots:
            rows = self._conn.execute(
                "SELECT campaign_id FROM campaign_slots"
                " WHERE region=? AND week_start=? AND campaign_id != ?",
                (s.region, s.week_start.isoformat(), exclude),
            ).fetchall()
            for (cid,) in rows:
                result.setdefault(cid, set()).add(s)
        return result

    # ---- 冲突 ----
    @staticmethod
    def _pair(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def _slots_of_conflict(self, conflict_id: int) -> set[RegionWeek]:
        rows = self._conn.execute(
            "SELECT region, week_start FROM conflict_slots WHERE conflict_id=?",
            (conflict_id,),
        ).fetchall()
        return {
            RegionWeek(region=r, week_start=date.fromisoformat(w)) for r, w in rows
        }

    def _row_to_episode(self, row: tuple) -> ConflictEpisode:
        cid, a, b, started, ended = row
        return ConflictEpisode(
            id=cid,
            campaign_a=a,
            campaign_b=b,
            started_at=datetime.fromisoformat(started),
            ended_at=datetime.fromisoformat(ended) if ended else None,
            slots=frozenset(self._slots_of_conflict(cid)),
        )

    def get_active_conflict(self, a: int, b: int) -> ConflictEpisode | None:
        lo, hi = self._pair(a, b)
        row = self._conn.execute(
            "SELECT id, campaign_a, campaign_b, started_at, ended_at"
            " FROM conflicts WHERE campaign_a=? AND campaign_b=?"
            " AND ended_at IS NULL",
            (lo, hi),
        ).fetchone()
        return self._row_to_episode(row) if row else None

    def insert_conflict(self, a: int, b: int, started_at: datetime,
                        slots: set[RegionWeek]) -> int:
        lo, hi = self._pair(a, b)
        cur = self._conn.execute(
            "INSERT INTO conflicts (campaign_a, campaign_b, started_at)"
            " VALUES (?, ?, ?)",
            (lo, hi, started_at.isoformat()),
        )
        conflict_id = cur.lastrowid
        self.update_conflict_slots(conflict_id, slots)
        return conflict_id

    def update_conflict_slots(self, conflict_id: int, slots: set[RegionWeek]) -> None:
        self._conn.execute(
            "DELETE FROM conflict_slots WHERE conflict_id=?", (conflict_id,)
        )
        self._conn.executemany(
            "INSERT INTO conflict_slots (conflict_id, region, week_start)"
            " VALUES (?, ?, ?)",
            [(conflict_id, s.region, s.week_start.isoformat()) for s in slots],
        )

    def end_conflict(self, conflict_id: int, ended_at: datetime) -> None:
        self._conn.execute(
            "UPDATE conflicts SET ended_at=? WHERE id=?",
            (ended_at.isoformat(), conflict_id),
        )
        self._conn.execute(
            "DELETE FROM conflict_slots WHERE conflict_id=?", (conflict_id,)
        )

    def get_active_conflicts_of(self, campaign_id: int) -> list[ConflictEpisode]:
        rows = self._conn.execute(
            "SELECT id, campaign_a, campaign_b, started_at, ended_at"
            " FROM conflicts WHERE ended_at IS NULL"
            " AND (campaign_a=? OR campaign_b=?) ORDER BY id",
            (campaign_id, campaign_id),
        ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def get_conflict_history(self, a: int, b: int) -> list[ConflictEpisode]:
        lo, hi = self._pair(a, b)
        rows = self._conn.execute(
            "SELECT id, campaign_a, campaign_b, started_at, ended_at"
            " FROM conflicts WHERE campaign_a=? AND campaign_b=?"
            " ORDER BY started_at, id",
            (lo, hi),
        ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    # ---- 通知 ----
    def insert_notification(self, conflict_id: int, recipient: str,
                            created_at: datetime) -> int:
        cur = self._conn.execute(
            "INSERT INTO notifications (conflict_id, recipient, created_at)"
            " VALUES (?, ?, ?)",
            (conflict_id, recipient, created_at.isoformat()),
        )
        return cur.lastrowid

    def list_notifications(self) -> list[Notification]:
        rows = self._conn.execute(
            "SELECT id, conflict_id, recipient, created_at"
            " FROM notifications ORDER BY id"
        ).fetchall()
        return [
            Notification(
                id=r[0],
                conflict_id=r[1],
                recipient=r[2],
                created_at=datetime.fromisoformat(r[3]),
            )
            for r in rows
        ]
