"""活动排期与冲突检测服务，供上层活动管理服务调用。"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime
from typing import Callable, TypeVar

from .models import (
    Campaign,
    CampaignStatus,
    ConflictEpisode,
    Notification,
    Recurrence,
)
from .recurrence import campaign_region_weeks
from .store import Store

MAX_SPAN_DAYS = 365
MAX_RECURRENCE_COUNT = 52
_MAX_LOCK_RETRIES = 100

#: update_campaign 中“保持原值”的哨兵（显式传 None 表示清除重复设置）
_KEEP = object()

_T = TypeVar("_T")


class ValidationError(ValueError):
    pass


class CampaignNotFoundError(KeyError):
    pass


class CampaignService:
    """线程安全：内部按线程持有连接，写操作串行化（BEGIN IMMEDIATE）。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()
        self._stores: list[Store] = []
        self._stores_lock = threading.Lock()
        if db_path != ":memory:":
            # 先在调用线程完成建表与 WAL 切换，避免多线程首次连接时争抢初始化
            Store(db_path).close()

    def _store(self) -> Store:
        store = getattr(self._local, "store", None)
        if store is None:
            store = Store(self._db_path)
            with self._stores_lock:
                self._stores.append(store)
            self._local.store = store
        return store

    def close(self) -> None:
        """关闭所有线程创建的连接。工作线程退出后由主线程统一调用。"""
        with self._stores_lock:
            stores, self._stores = self._stores, []
        for store in stores:
            store.close()
        self._local.store = None

    def _write_txn(self, fn: Callable[[Store], _T]) -> _T:
        """在 BEGIN IMMEDIATE 事务中执行 fn（串行化写），锁冲突时整体重试。

        重试针对的是 SQLite 为避免死锁而不经 busy_timeout 直接返回的
        “database is locked”；fn 本身不产生副作用之外的中间状态，
        失败即回滚，要么全部生效要么全部不生效。
        """
        store = self._store()
        delay = 0.001
        for attempt in range(_MAX_LOCK_RETRIES):
            try:
                store.begin_immediate()
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt + 1 < _MAX_LOCK_RETRIES:
                    time.sleep(delay)
                    delay = min(delay * 2, 0.1)
                    continue
                raise
            try:
                result = fn(store)
                store.commit()
                return result
            except sqlite3.OperationalError as e:
                store.rollback()
                if "locked" in str(e) and attempt + 1 < _MAX_LOCK_RETRIES:
                    time.sleep(delay)
                    delay = min(delay * 2, 0.1)
                    continue
                raise
            except BaseException:
                store.rollback()
                raise
        raise sqlite3.OperationalError("database is locked (retries exhausted)")

    # ---- 校验 ----
    @staticmethod
    def _validate(manager: str, regions: set[str], start_date: date,
                  end_date: date, recurrence: Recurrence | None) -> None:
        if not manager:
            raise ValidationError("manager 不能为空")
        if not regions:
            raise ValidationError("regions 不能为空")
        if end_date < start_date:
            raise ValidationError("end_date 不能早于 start_date")
        if (end_date - start_date).days + 1 > MAX_SPAN_DAYS:
            raise ValidationError(f"活动跨度不能超过 {MAX_SPAN_DAYS} 天")
        if recurrence is not None and not (1 <= recurrence.count <= MAX_RECURRENCE_COUNT):
            raise ValidationError(f"重复次数须在 1..{MAX_RECURRENCE_COUNT} 之间")

    # ---- 冲突重算（须在写事务内调用） ----
    def _recompute_conflicts(self, store: Store, campaign: Campaign,
                             old_slots: set, new_slots: set) -> None:
        now = datetime.now()
        partners = store.campaigns_sharing_slots(old_slots | new_slots,
                                                 exclude=campaign.id)
        for partner_id in partners:
            partner = store.get_campaign(partner_id)
            if partner is None:
                continue
            common = new_slots & store.get_slots(partner_id)
            active = store.get_active_conflict(campaign.id, partner_id)
            if common:
                if active is None:
                    conflict_id = store.insert_conflict(
                        campaign.id, partner_id, now, common)
                    recipients = {campaign.manager, partner.manager}
                    for recipient in sorted(recipients):
                        store.insert_notification(conflict_id, recipient, now)
                elif active.slots != common:
                    store.update_conflict_slots(active.id, common)
            elif active is not None:
                store.end_conflict(active.id, now)

    # ---- 写操作 ----
    def create_campaign(
        self,
        name: str,
        manager: str,
        regions: set[str],
        start_date: date,
        end_date: date,
        status: CampaignStatus,
        recurrence: Recurrence | None = None,
    ) -> int:
        """新建活动并检测冲突，返回活动 id。冲突变化与通知同事务生效。"""
        self._validate(manager, regions, start_date, end_date, recurrence)
        campaign = Campaign(
            id=0,
            name=name,
            manager=manager,
            regions=frozenset(regions),
            start_date=start_date,
            end_date=end_date,
            status=status,
            recurrence=recurrence,
        )

        def txn(store: Store) -> int:
            campaign.id = store.insert_campaign(campaign)
            slots = campaign_region_weeks(campaign)
            store.replace_slots(campaign.id, slots)
            self._recompute_conflicts(store, campaign, set(), slots)
            return campaign.id

        return self._write_txn(txn)

    def update_campaign(
        self,
        campaign_id: int,
        *,
        name: str | None = None,
        manager: str | None = None,
        regions: set[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        status: CampaignStatus | None = None,
        recurrence: Recurrence | None | object = _KEEP,
    ) -> None:
        """修改活动并重算它涉及的所有冲突。"""
        def txn(store: Store) -> None:
            current = store.get_campaign(campaign_id)
            if current is None:
                raise CampaignNotFoundError(campaign_id)
            updated = Campaign(
                id=current.id,
                name=name if name is not None else current.name,
                manager=manager if manager is not None else current.manager,
                regions=frozenset(regions) if regions is not None else current.regions,
                start_date=start_date if start_date is not None else current.start_date,
                end_date=end_date if end_date is not None else current.end_date,
                status=status if status is not None else current.status,
                recurrence=current.recurrence if recurrence is _KEEP else recurrence,
            )
            self._validate(updated.manager, set(updated.regions),
                           updated.start_date, updated.end_date,
                           updated.recurrence)
            old_slots = store.get_slots(campaign_id)
            new_slots = campaign_region_weeks(updated)
            store.update_campaign(updated)
            store.replace_slots(campaign_id, new_slots)
            self._recompute_conflicts(store, updated, old_slots, new_slots)

        self._write_txn(txn)

    # ---- 查询 ----
    def get_campaign(self, campaign_id: int) -> Campaign:
        campaign = self._store().get_campaign(campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        return campaign

    def get_current_conflicts(self, campaign_id: int) -> list[ConflictEpisode]:
        """某活动当前的全部冲突（含各自撞在哪些区域周）。"""
        return self._store().get_active_conflicts_of(campaign_id)

    def get_conflict_history(self, campaign_a: int, campaign_b: int) -> list[ConflictEpisode]:
        """任意一对活动的冲突历史：每段何时开始、何时结束。"""
        return self._store().get_conflict_history(campaign_a, campaign_b)

    def list_notifications(self) -> list[Notification]:
        return self._store().list_notifications()
