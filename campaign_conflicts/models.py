"""数据类型定义：活动、重复规则、冲突、通知。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class CampaignStatus(enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


#: 只有这三种状态参与冲突判断
CONFLICTING_STATUSES = frozenset(
    {CampaignStatus.SCHEDULED, CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)


class RecurrenceFreq(enum.Enum):
    WEEKLY = "weekly"      # 每周，间隔 7 天
    BIWEEKLY = "biweekly"  # 每两周，间隔 14 天
    MONTHLY = "monthly"    # 每月，同一日号（无此日则落当月最后一天）


@dataclass(frozen=True)
class Recurrence:
    freq: RecurrenceFreq
    count: int  # 共 N 次，1 <= N <= 52


@dataclass
class Campaign:
    id: int
    name: str
    manager: str
    regions: frozenset[str]
    start_date: date
    end_date: date  # 首尾都算，跨度不超过 365 天
    status: CampaignStatus
    recurrence: Recurrence | None = None


@dataclass(frozen=True, order=True)
class RegionWeek:
    """一个区域 + 一周（week_start 为周一）。"""

    region: str
    week_start: date


@dataclass
class ConflictEpisode:
    """一对活动之间的一段冲突。"""

    id: int
    campaign_a: int  # 较小的活动 id
    campaign_b: int
    started_at: datetime
    ended_at: datetime | None  # None 表示冲突仍在持续
    slots: frozenset[RegionWeek] = field(default_factory=frozenset)


@dataclass
class Notification:
    id: int
    conflict_id: int
    recipient: str  # 负责人
    created_at: datetime
