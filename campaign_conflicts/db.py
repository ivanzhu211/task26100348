"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,
    recurrence_frequency TEXT,
    recurrence_occurrences INTEGER
);

CREATE TABLE IF NOT EXISTS campaign_regions (
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    region TEXT NOT NULL,
    PRIMARY KEY (campaign_id, region)
);

-- Materialized (region, week) cells occupied by each campaign,
-- across all of its recurrence occurrences. Only maintained for
-- campaigns in a conflicting status.
CREATE TABLE IF NOT EXISTS campaign_cells (
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    region TEXT NOT NULL,
    week_start TEXT NOT NULL,  -- Monday of the week, ISO date
    PRIMARY KEY (campaign_id, region, week_start)
);
CREATE INDEX IF NOT EXISTS idx_cells_region_week
    ON campaign_cells (region, week_start);

-- One row per conflict segment between a pair of campaigns.
-- campaign_id_a < campaign_id_b always.
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id_a INTEGER NOT NULL,
    campaign_id_b INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conflicts_pair
    ON conflicts (campaign_id_a, campaign_id_b);
-- At most one open (ended_at IS NULL) segment per pair.
CREATE UNIQUE INDEX IF NOT EXISTS idx_conflicts_open_pair
    ON conflicts (campaign_id_a, campaign_id_b) WHERE ended_at IS NULL;

-- Current cells of each open conflict segment (recomputed on change).
CREATE TABLE IF NOT EXISTS conflict_cells (
    conflict_id INTEGER NOT NULL REFERENCES conflicts(id),
    region TEXT NOT NULL,
    week_start TEXT NOT NULL,
    PRIMARY KEY (conflict_id, region, week_start)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id TEXT NOT NULL,
    conflict_id INTEGER NOT NULL REFERENCES conflicts(id),
    campaign_id INTEGER NOT NULL,
    other_campaign_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_recipient
    ON notifications (recipient_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with the settings the library relies on."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL keeps atomicity even with NORMAL; avoids fsync stalls on slow disks.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
