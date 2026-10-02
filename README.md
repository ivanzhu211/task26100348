# campaign-conflicts

营销活动排期冲突检测库。由上层活动管理服务直接调用（无接口层），
活动、冲突记录、通知全部持久化在 SQLite。

## 业务规则

- 两个活动的某一次投放落在**同一周（周一至周日）**且**目标区域有交集**即冲突，
  日期本身不需要重叠；同一活动的多次重复之间不算冲突。
- 只有 `SCHEDULED` / `ACTIVE` / `PAUSED` 状态参与冲突判断。
- 重复规则：每周（7 天）、每两周（14 天）、每月（同首次日号，当月无此日落
  当月最后一天，后续月份回到原日号），共 N 次（N ≤ 52）。
- 冲突按一对活动记为“段”：共同区域周全部消失则该段结束；结束后又撞上算新的一段；
  持续期间共同区域周变化仍属同一段。
- 每段新冲突给双方负责人各发一条站内通知（同一负责人只发一条），持续期间不重复通知。

## 用法

```python
from datetime import date
from campaign_conflicts import (
    CampaignService, CampaignStatus, Recurrence, RecurrenceFreq,
)

svc = CampaignService("campaigns.db")   # 或 ":memory:"

a = svc.create_campaign(
    name="春季促销", manager="alice", regions={"华东", "华北"},
    start_date=date(2026, 3, 2), end_date=date(2026, 3, 8),
    status=CampaignStatus.SCHEDULED,
    recurrence=Recurrence(RecurrenceFreq.WEEKLY, 4),   # 可选
)
b = svc.create_campaign(
    name="品牌曝光", manager="bob", regions={"华东"},
    start_date=date(2026, 3, 5), end_date=date(2026, 3, 6),
    status=CampaignStatus.SCHEDULED,
)

svc.get_current_conflicts(a)     # a 当前的全部冲突（含撞在哪些区域周）
svc.get_conflict_history(a, b)   # a、b 的冲突历史：每段何时开始/结束
svc.list_notifications()         # 已发出的站内通知

svc.update_campaign(b, regions={"西南"})          # 改区域，冲突自动重算
svc.update_campaign(a, recurrence=None)           # 显式清除重复设置
svc.update_campaign(a, status=CampaignStatus.PAUSED)

svc.close()
```

## 设计要点

- `campaign_slots` 表冗余每个活动当前占用的区域周，并以 `(region, week_start)`
  建索引：单次新建/修改只重算该活动涉及的活动对，耗时与活动总数无关。
- 写操作整体包在 `BEGIN IMMEDIATE` 事务里：多线程/多连接提交被串行化，
  结果等价于某个先后顺序；一次修改连同冲突变化和通知要么全部生效要么回滚。
- 冲突段（`conflicts`）与其当前区域周（`conflict_slots`）分表存储，
  查某活动当前冲突直接读索引，即时返回；历史段保留 `started_at`/`ended_at`。
- SQLite 开启 WAL + `synchronous=NORMAL`，读不阻塞写。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_recurrence.py`：重复展开（含月末/闰年）、区域周计算。
- `tests/test_service.py`：冲突段生命周期、通知去重、参数校验。
- `tests/test_random_ops.py`：随机操作序列每步与从头全量重算逐段比对（验收一）。
- `tests/test_concurrency.py`：多线程同区域周新建，两两冲突且每段恰好通知一次（验收二）。
- `tests/test_performance.py`：1 千 → 2 万活动，单次写/查耗时不明显变长。
