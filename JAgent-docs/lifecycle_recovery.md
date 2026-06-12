# Harness 生命周期恢复机制 — 服务器重启 + 孤儿 Run 处理

> **当前阶段**: 设计文档（待审查）
> **关联里程碑**: V1.0 — 生产就绪（重启恢复）
> **文档版本**: v1.0
> **最后更新**: 2026-06-08

---

## 1. 背景

### 1.1 问题

当前 `BaseScheduler` 的生命周期控制完全基于**内存状态**：

| 内存状态 | 位置 | 用途 |
|----------|------|------|
| `_running_tasks: dict[str, asyncio.Task]` | `BaseScheduler` | 追踪正在运行的调度循环 |
| `_pause_events: dict[str, asyncio.Event]` | `BaseScheduler` | 暂停/恢复信号 |
| `_cancel_flags: dict[str, asyncio.Event]` | `BaseScheduler` | 取消信号 |
| `_schedulers: dict[str, PlanningExecutorScheduler]` | `HarnessAPI` | API 层查找活着的调度器 |

**服务器重启后，这四个数据结构全部丢失。** 但 Event Store 中仍有完整的事件流，fold 后状态仍为 `RUNNING` 或 `PAUSED`。结果：

- 没有 Scheduler 驱动这些 Run 前进
- 也没有机制标记它们"已死"
- 前端看到的是"运行中"但永远不会结束

### 1.2 范围

| 包括 | 不包括 |
|------|--------|
| 服务器重启、进程崩溃后的孤儿 Run 检测 | 分布式 Multi-Worker 架构的 Keepalive |
| 孤儿 Run 的标记（`RunOrphaned` 事件） | 活着的 Scheduler 的 in-memory 心跳机制 |
| 用户决策端点（`abandon` / `retry`） | 自动断点续跑（需要确定性校验，方案不在此设计） |
| Checkpoint 恢复的抽取封装 | 容器级热迁移 |
| API 层的孤儿感知 | |

### 1.3 已有机制

| 机制 | 现有能力 | 不足 |
|------|----------|------|
| `fold_events()` | 从事件流重建 `RunState` | 只能反映过去，不能判断有无活着的 Scheduler |
| `ContextManager.find_resume_seq()` | 找到最新的 `ContextCheckpointed` 事件 seq | 只在 `_ensure_run_started` 中调用，不处理无 checkpoint 场景 |
| `BaseScheduler._ensure_run_started()` | 写 `RunStarted` + 日志 checkpoint 恢复点 | 只在 Scheduler 主动启动时调用，服务器重启后没有 Scheduler 启动它 |
| `HarnessAPI._schedulers` | 追踪活的调度器 | 全内存，重启后为空 |

---

## 2. 设计原则

### 2.1 核心约束

| 原则 | 含义 |
|------|-------|
| **系统强制不猜测** | 不自作用户决定 resume 还是 fail。只记录"检测到重启" |
| **事件是事实** | `RunOrphaned` 事件写入 Store，`fold_events` 基于它推演 `orphaned` 标记 |
| **给用户选择权** | 提供 `abandon`（放弃）和 `retry`（重试）两个端点 |
| **幂等** | 重复重启不会写入重复的 `RunOrphaned` 事件 |
| **不破坏现有生命周期** | 不改 `BaseScheduler` 的 `pause/resume/cancel/_fail/_complete` |
| **归置正确** | 检测逻辑不在 `BaseScheduler` 实例上（重启后没有实例）。放在独立的 `lifecycle.py` 模块 |

### 2.2 检测 vs 决策分离

```
检测层（lifecycle.py）:
  输入: EventStore
  逻辑: 折叠事件 → 判断 RUNNING/PAUSED → 写入 RUN_ORPHANED
  输出: orphaned_run_ids

决策层（routes.py）:
  输入: orphaned run_id + 用户动作 (abandon/retry)
  逻辑: 校验 orphaned → 写 RUN_FAILED / 建新 RUN
  输出: success / new_run_id
```

---

## 3. 详细设计

### 3.1 新增事件类型: `RunOrphaned`

**文件**: `harness/models/events.py`

```python
class EventType(str, Enum):
    # ... 现有 24 个事件 ...
    RUN_ORPHANED = "RunOrphaned"          # ← 新增

class RunOrphanedPayload(BaseModel):
    reason: str       # "server_restart"，将来可扩展 "scheduler_crash" 等
    last_seq: int     # 检测时的最新 seq
    timestamp: float  # 检测时间
```

注册到 `PAYLOAD_MODEL_MAP`。

### 3.2 折叠感知: `orphaned` 标记

**文件**: `harness/core/fold.py`

```python
@dataclass
class RunState:
    # ... 现有 16 个字段 ...
    orphaned: bool = False    # ← 新增
```

在 `fold_events` 的 match 中添加：

```python
case EventType.RUN_ORPHANED:
    state.orphaned = True
    # 不改 state.status — 状态仍然是 RUNNING 或 PAUSED
```

**关键设计**: `orphaned` 和 `status` 是正交的。一个 run 可以是 `RUNNING + orphaned`（意味着它本在运行，但调度器死了），也可以是 `RUNNING + not orphaned`（正常状态）。前端通过 `orphaned` 判断是否展示特殊提示。

### 3.3 核心检测逻辑

**新文件**: `harness/core/lifecycle.py`

```python
"""Server lifecycle management — orphan detection, recovery decisions.

与 BaseScheduler 解耦：BaseScheduler 是活着的 Scheduler 的运行时生命周期，
此模块处理"没有 Scheduler"时的生命周期问题（服务器重启、进程崩溃）。
"""

from harness.core.fold import RunStatus, fold_events
from harness.models.events import (
    EventType, RunOrphanedPayload, RunFailedPayload, RunStartedPayload,
)

async def detect_orphan_candidates(store) -> list[str]:
    """纯查询：找到所有需要标记为孤儿的 Run。（无副作用，幂等）"""
    run_ids = await store.list_all_run_ids()
    candidates = []
    for rid in run_ids:
        events = await store.get_events(rid)
        state = fold_events(events)
        if state.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
            continue
        if state.orphaned:
            continue   # 已标记过，幂等
        candidates.append(rid)
    return candidates

async def mark_orphans(store, reason="server_restart") -> list[str]:
    """写 RUN_ORPHANED 事件。幂等。返回实际写入的 run_id 列表。"""
    orphaned = []
    for rid in await detect_orphan_candidates(store):
        events = await store.get_events(rid)
        state = fold_events(events)
        await store.append_event(
            rid, EventType.RUN_ORPHANED,
            RunOrphanedPayload(reason=reason, last_seq=state.seq,
                               timestamp=time.time()).model_dump(),
        )
        orphaned.append(rid)
    return orphaned

async def abandon_run(store, run_id) -> None:
    """放弃孤儿 Run → 写 RUN_FAILED。仅允许 orphaned==True 的 run。"""
    events = await store.get_events(run_id)
    if not events:
        raise ValueError("Run not found")
    state = fold_events(events)
    if not state.orphaned:
        raise ValueError("Run is not orphaned")
    await store.append_event(
        run_id, EventType.RUN_FAILED,
        RunFailedPayload(
            final_error="abandoned after server restart",
            event_count=len(events),
            result_summary=f"Run abandoned. {len(state.thought_history)} thought(s), "
                           f"{len(state.tool_results)} tool call(s).",
        ).model_dump(),
    )

async def retry_run(store, run_id, start_scheduler) -> str:
    """重试孤儿 Run → 创建新 Run 并启动调度器。返回新 run_id。"""
    events = await store.get_events(run_id)
    if not events:
        raise ValueError("Run not found")
    state = fold_events(events)
    if not state.orphaned:
        raise ValueError("Run is not orphaned")

    new_id = uuid4().hex[:8]
    await store.append_event(
        new_id, EventType.RUN_STARTED,
        RunStartedPayload(
            intent=state.intent,
            context_snapshot={"retry_of": run_id},
        ).model_dump(),
    )
    await start_scheduler(new_id, state.intent)
    return new_id
```

**设计的通用性**:
- `reason` 参数支持扩展（如 `scheduler_crash`, `node_failure`）
- 不依赖任何 Scheduler 实例
- `mark_orphans` 是幂等的（`detect_orphan_candidates` 跳过 `orphaned==True` 的 run）

### 3.4 `_try_checkpoint_recovery` — BaseScheduler 的补充

**文件**: `harness/core/scheduler.py`

这是唯一真正需要在 `BaseScheduler` 上加的方法——因为它用到了 `self.context_manager`。

```python
class BaseScheduler(ABC):
    async def _try_checkpoint_recovery(self, events: list[Event]) -> bool:
        """尝试从 checkpoint 恢复。从 _ensure_run_started 中抽取。

        返回 True 表示有 checkpoint，可以跳过已折叠的事件继续。
        返回 False 表示没有 checkpoint，需从头折叠。
        """
        if not self.context_manager:
            return False
        cp = self.context_manager.find_resume_seq(events)
        if cp > 0:
            _sched_ctrl.info("Resuming from seq %d (checkpoint)", cp)
        return cp > 0
```

`_ensure_run_started` 中原有的内联 checkpoint 逻辑（当前行 278-281）替换为此方法调用，行为不变。

### 3.5 EventStore 新增方法

**文件**: `harness/storage/event_store.py`

```python
async def list_all_run_ids(self) -> list[str]:
    """返回所有 run_id，不分页。供启动扫描用。"""
    cursor = await self.conn.execute(
        "SELECT DISTINCT run_id FROM events ORDER BY run_id"
    )
    rows = await cursor.fetchall()
    return [r["run_id"] for r in rows]
```

### 3.6 app.py 接入

**文件**: `harness/api/app.py`

```python
from harness.core.lifecycle import mark_orphans

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        api = get_hapi()
    except RuntimeError:
        yield
        return
    
    await api.store.initialize()

    # 新增：标记孤儿 Run
    orphaned = await mark_orphans(api.store)
    if orphaned:
        logger.warning("标记了 %d 个孤儿 Run: %s", len(orphaned), orphaned)

    try:
        yield
    finally:
        await api.store.close()
```

### 3.7 API 端点

**文件**: `harness/api/routes.py`

#### 3.7.1 `POST /api/v1/runs/{run_id}/abandon`

```python
@router.post("/api/v1/runs/{run_id}/abandon")
async def abandon_run(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    try:
        await lifecycle.abandon_run(api.store, run_id)
        return {"success": True}
    except ValueError as e:
        return JSONResponse(status_code=409 if "not orphaned" in str(e) else 404,
                            content={"error": str(e)})
```

**权限** — 仅允许 `orphaned == True` 的 run。非孤儿 Run 应使用现有的 `DELETE /api/v1/runs/{run_id}`。
**副作用** — 写 `RUN_FAILED` 事件，run 进入 `FAILED` 终态。

#### 3.7.2 `POST /api/v1/runs/{run_id}/retry`

```python
@router.post("/api/v1/runs/{run_id}/retry")
async def retry_run(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    try:
        new_id = await lifecycle.retry_run(
            api.store, run_id, lambda rid, intent: api.start_run(rid, intent),
        )
        return {"run_id": new_id, "retry_of": run_id}
    except ValueError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
```

**权限** — 仅允许 `orphaned == True` 的 run。
**副作用** — 创建新 Run + 启动新 Scheduler。原 Run 仍在 Event Store 中保持 `RUNNING + orphaned`。

#### 3.7.3 `list_runs` 加 `orphaned` 字段

```python
summaries.append({
    # ... 现有字段 ...
    "orphaned": state.orphaned,       # ← 新增
})
```

前端不用多发一个请求就能识别孤儿 Run。

#### 3.7.4 `confirm_run` 检查 orphaned

```python
@router.post("/api/v1/runs/{run_id}/confirm")
async def confirm_run(run_id, body, api):
    events = await api.store.get_events(run_id)
    state = fold_events(events)
    if state.orphaned:
        return JSONResponse(
            status_code=409,
            content={"error": "Run is orphaned (server restarted). "
                              "Abandon or retry first."},
        )
    # ... 现有确认逻辑不变 ...
```

防止用户在孤儿 Run 上确认——确认了也没 Scheduler 来响应。

---

## 4. 边界场景分析

### 4.1 连续重启

| 场景 | 表现 |
|------|------|
| 第一次重启 → `mark_orphans` 写 `RUN_ORPHANED` | ✅ |
| 第二次重启 → `detect_orphan_candidates` 看到 `orphaned==True`，跳过 | ✅ 幂等 |

### 4.2 PAUSED 状态重启

Run 在 pause 状态时重启 → fold 后 `status == PAUSED`，在检测范围内 → 写入 `RUN_ORPHANED` → 用户看到 PAUSED + orphaned 标记。用户可选择 abandon（写 `RUN_FAILED`）或 retry（新 Run）。

### 4.3 等待确认时重启

事件流中有 `CONFIRMATION_REQUESTED` 但没有 `CONFIRMATION_RECEIVED` → `fold_events` 后 `pending_confirmations` 非空 → `status == RUNNING` → 被检测为孤儿。

用户行为：
- 可以看到待确认列表
- `confirm` 返回 409（孤儿 Run 不能确认）
- 只能 abandon 或 retry

### 4.4 工具执行中重启

事件流最后一条是 `TOOL_CALLED`，没有匹配的 `TOOL_COMPLETED`/`TOOL_FAILED`/`TOOL_TIMEOUT`。

- fold 后 `status == RUNNING` → 被检测为孤儿
- 那条 `TOOL_CALLED` 没有匹配 completion → 工具执行结果未知
- `retry` 创建新 Run 从头开始
- 旧 Run 的 `TOOL_CALLED` 不会影响新 Run 的执行（新 Run 有独立的事件流）

**安全说明**: 对于非幂等工具（`side_effects=[WRITE]`），retry 可能重复副作用。这是用户主动选择 retry 时的已知风险，与创建新 Run 后手动描述相同场景等价。

### 4.5 确认孤儿 Run（`confirm` 被拒）

确认孤儿 Run 没有意义（没有 Scheduler 来响应确认）。`confirm` 端点检查 `orphaned` 并返回 409，防止用户浪费时间。

### 4.6 重试途中又重启

新 Run 创建后被第二次重启 → `mark_orphans` 会扫描到新 Run 的状态（`RUNNING`）→ 写入 `RUN_ORPHANED` → 和旧 Run 一样处理。幂等性无问题。

### 4.7 非孤儿 Run 调用 abandon

返回 409，要求使用现有的 `DELETE` 端点。

---

## 5. 文件改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `harness/models/events.py` | EventType 加 `RUN_ORPHANED` + `RunOrphanedPayload` + PAYLOAD_MODEL_MAP | ~15 |
| `harness/core/fold.py` | `RunState.orphaned` 字段 + fold case | ~5 |
| `harness/core/lifecycle.py` | **新文件**: `detect_orphan_candidates`, `mark_orphans`, `abandon_run`, `retry_run` | ~70 |
| `harness/core/scheduler.py` | `_try_checkpoint_recovery()` 方法 + `_ensure_run_started` 替换内联 | ~10 |
| `harness/storage/event_store.py` | `list_all_run_ids()` 方法 | ~8 |
| `harness/api/app.py` | lifespan 调用 `mark_orphans` | ~5 |
| `harness/api/routes.py` | `abandon` + `retry` 端点 + `list_runs` 加 `orphaned` + `confirm` 检查 orphaned | ~60 |
| `harness/api/schemas.py` | 可能在 list/confirm 需要补充字段（如 ConfirmRequest 变更） | ~5 |
| `tests/test_lifecycle.py` | **新文件**: 测试孤儿检测、abandon、retry、幂等性、边界场景 | ~200 |

**总计**: ~378 行代码（含测试）

### 不改动的文件

| 文件 | 原因 |
|------|------|
| `harness/core/context_manager.py` | Checkpoint 机制已完整，`find_resume_seq` 只是被抽取封装 |
| `harness/api/ws.py` | 前端 WS 自动重连，新事件正常广播，不需要改 |
| `harness/monitoring/run_monitor.py` | `cleanup` 被 `run()` finally 调用，不涉及服务器重启 |
| `harness/api/serve.py` | Assembly 逻辑不变，只是 `app.py` lifespan 增加了扫描 |
| `harness/tools/*.py` | 工具层感知不到生命周期变化 |

---

## 6. 状态迁移图

```
服务器重启前               服务器重启后                   用户操作后

  RUNNING ──→ RUNNING + orphaned ──abandon──→ FAILED
                                │
                                └──retry──→ 新 RUN (RUNNING)

  PAUSED  ──→ PAUSED  + orphaned ──abandon──→ FAILED
                                │
                                └──retry──→ 新 RUN (RUNNING)

  COMPLETED ─→ COMPLETED          （不受影响）
  FAILED   ─→ FAILED              （不受影响）
```

---

## 7. 测试计划

**新文件**: `tests/test_lifecycle.py`

| # | 测试 | 类型 | 验证内容 |
|---|------|------|----------|
| 1 | `test_detect_running_candidate` | 单元 | RUNNING 无 RUN_ORPHANED → 进入候选列表 |
| 2 | `test_detect_paused_candidate` | 单元 | PAUSED 无 RUN_ORPHANED → 进入候选列表 |
| 3 | `test_detect_skips_completed` | 单元 | COMPLETED → 跳过 |
| 4 | `test_detect_skips_failed` | 单元 | FAILED → 跳过 |
| 5 | `test_detect_skips_already_orphaned` | 单元 | 已有 RUN_ORPHANED → 跳过（幂等） |
| 6 | `test_mark_orphans_writes_event` | 集成 | `mark_orphans` → EventStore 查到 `RUN_ORPHANED` |
| 7 | `test_mark_orphans_idempotent` | 集成 | 两次调用只写一次 `RUN_ORPHANED` |
| 8 | `test_fold_sets_orphaned_flag` | 单元 | fold 含 RUN_ORPHANED → `state.orphaned == True` |
| 9 | `test_abandon_success` | 集成 | abandon 孤儿 Run → `RunFailed` 被写入 |
| 10 | `test_abandon_non_orphan_rejected` | 集成 | 非孤儿 Run → `ValueError` |
| 11 | `test_abandon_nonexistent_run` | 集成 | 不存在 Run → `ValueError` |
| 12 | `test_retry_creates_new_run` | 集成 | retry → 新 RunStarted + start_scheduler 被调用 |
| 13 | `test_retry_non_orphan_rejected` | 集成 | 非孤儿 Run → `ValueError` |
| 14 | `test_checkpoint_recovery_no_cm` | 单元 | 无 ContextManager → False |
| 15 | `test_checkpoint_recovery_with_cp` | 单元 | 有 ContextCheckpointed → True |
| 16 | `test_checkpoint_recovery_no_cp` | 单元 | 无 Checkpointed → False |

---

## 8. 架构决策记录

### ADR-1: 为什么不在 `BaseScheduler` 上做检测？

服务器重启后 `BaseScheduler` 实例不存在。检测逻辑只需要 `EventStore` 和 `fold_events`，不需要 Scheduler 的任何状态。放在独立的 `lifecycle.py` 中更符合单一职责。

### ADR-2: 为什么需要一个独立事件 `RunOrphaned` 而不是动态推导？

如果 fold_events 动态检测"最后一个事件超过 30 秒"判定孤儿，依赖时间戳，不可靠（可能是网络延迟、GC 暂停）。写入事件是持久的事实，不受时间影响。

### ADR-3: 为什么 `retry` 创建新 Run 而不是恢复旧 Run？

恢复旧 Run 需要确定重启前执行到了哪一步。如果最后一条事件是 `TOOL_CALLED`（没完成），工具可能已执行了一半副作用。没有确定的恢复点。创建新 Run 从零开始是最安全的选择。

### ADR-4: `orphaned` 为什么不改 `status`？

`status` 反映"Run 在事件流中的逻辑状态"（fold_events 的结果）。`orphaned` 反映"是否与活着的 Scheduler 失联"。两者正交。不改 status 确保：
1. `pending_confirmations` 等字段不受影响
2. `pause_reason` 不受影响
3. 前端可以通过 `orphaned` 标记单独展示特殊 UI

---
