"""Data model definitions for the campaign conflict detection library."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Optional


class CampaignStatus(enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


#: Statuses that participate in conflict detection.
CONFLICTING_STATUSES = frozenset(
    {CampaignStatus.SCHEDULED, CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)


class RecurrenceFrequency(enum.Enum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


MAX_SPAN_DAYS = 365
MAX_OCCURRENCES = 52


@dataclass(frozen=True)
class RecurrenceRule:
    frequency: RecurrenceFrequency
    occurrences: int  # total number of occurrences, 1..MAX_OCCURRENCES


@dataclass(frozen=True)
class Campaign:
    id: int
    owner_id: str
    regions: frozenset
    start_date: date
    end_date: date  # inclusive
    status: CampaignStatus
    recurrence: Optional[RecurrenceRule] = None


@dataclass(frozen=True)
class RegionWeek:
    """A (region, ISO week) cell identified by the Monday of the week."""

    region: str
    week_start: date  # Monday


@dataclass(frozen=True)
class ConflictSegment:
    """One continuous segment of conflict between a pair of campaigns."""

    id: int
    campaign_id_a: int
    campaign_id_b: int
    started_at: str
    ended_at: Optional[str]


@dataclass(frozen=True)
class Notification:
    id: int
    recipient_id: str
    conflict_id: int
    campaign_id: int
    other_campaign_id: int
    created_at: str
