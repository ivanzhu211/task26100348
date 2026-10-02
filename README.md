# campaign-conflicts

营销活动排期冲突检测库。由上层活动管理服务直接调用（无接口层），
活动、冲突记录与通知全部持久化在 SQLite 中。

## 功能

- 活动新建/修改时即时检测冲突：两个活动各自的某一次重复落在同一周
  （周一至周日，哪怕只占一天）且目标区域有交集，即在该区域周上冲突。
- 支持每周 / 每两周 / 每月重复（最多 52 次）；按月重复落在同一日号，
  当月无此日则落在当月最后一天，之后回到原日号。
- 仅已排期、进行中、已暂停三种状态参与冲突判断。
- 冲突按活动对分段记录：共同区域周全部消失则该段结束；再次撞上算新的一段。
  每段新冲突给双方负责人各发一条站内通知（同一负责人只发一条），
  段持续期间不重复通知。
- 每次写操作连同冲突变化与通知在单个事务中提交，要么全部生效要么全部不生效；
  多线程/多进程并发写由 `BEGIN IMMEDIATE` 串行化，结果等价于某个串行顺序。

## 使用

```python
from datetime import date
from campaign_conflicts import (
    CampaignService, CampaignStatus, RecurrenceRule, RecurrenceFrequency,
)

svc = CampaignService("campaigns.db")

a = svc.create_campaign(
    owner_id="alice",
    regions={"华东", "华北"},
    start_date=date(2026, 3, 2),
    end_date=date(2026, 3, 4),
    status=CampaignStatus.SCHEDULED,
    recurrence=RecurrenceRule(RecurrenceFrequency.WEEKLY, 4),  # 可选
)

svc.update_campaign(a, regions={"华东"}, status=CampaignStatus.PAUSED)

svc.current_conflicts(a)        # 该活动当前全部冲突（含具体区域周）
svc.conflict_history(a, b)      # 一对活动的冲突历史（每段起止时间）
svc.notifications_for("alice")  # 某负责人收到的通知
```

## 实现要点

- `campaign_conflicts/recurrence.py`：把活动展开为占用的 (区域, 周) 单元格。
- `campaign_conflicts/service.py`：写路径在事务内重建本活动单元格，
  通过 `campaign_cells` 的区域周索引反查相撞的其他活动，增量开/关冲突段；
  读路径直接查询已物化的冲突段，即时返回。
- `campaign_conflicts/db.py`：SQLite schema（WAL 模式）。

## 测试

```bash
python3 -m pytest tests/ -q
```

- `tests/test_randomized.py`：随机操作序列，每步后与从头重算的结果逐对对比，
  并校验通知与冲突历史逐段一致。
- `tests/test_concurrency.py`：多线程同时往同一区域周新建活动，
  验证两两冲突且每段恰好通知一次；混合并发写后终态与 oracle 一致。
- `tests/test_recurrence.py` / `tests/test_service.py`：单元与行为测试。
- `tests/test_performance.py`：活动数从 1k 涨到 4k 时单次操作耗时不明显变长。
