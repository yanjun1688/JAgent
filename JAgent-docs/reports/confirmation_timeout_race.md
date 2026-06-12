# 高危操作确认超时竞态条件审查报告

> **日期**: 2026-06-11
> **范围**: 完整追踪 high-risk tool → CONFIRMATION_NEEDED → RUN_PAUSED → 用户确认 → resume 的超时链路
> **状态**: 已修复（CT-1, CT-9）

---

## 一、背景

当工具标记为 `requires_confirmation=True` 或 Guardrail 触发确认时，ToolExecutor 返回 `CONFIRMATION_NEEDED`，调度器写入 `RUN_PAUSED` 并调用 `_wait_for_resume()` 等待操作员确认。超时由 `SchedulerConfig.pause_timeout_ms`（默认 300s = 5 分钟）控制。

---

## 二、架构概览

```
ToolExecutor.execute()
  └─ CONFIRMATION_NEEDED 状态返回
       ├─ AgentLoopScheduler._run_tool_call  (scheduler.py:342)  串行路径
       ├─ _execute_dynamic_plan               (scheduler.py:762)  DAG 动态
       └─ _execute_static_plan                (scheduler.py:911)  DAG 静态
              └─ while True:
                   ├─ append_event(RUN_PAUSED)
                   ├─ _wait_for_resume()  ← 超时发生处
                   ├─ refresh_state() → 检查 FAILED/COMPLETED
                   └─ executor.execute() / retry_step()

用户点击"同意" → POST /api/v1/runs/{id}/confirm
  ├─ append_event(CONFIRMATION_RECEIVED)
  └─ scheduler.resume()
       ├─ append_event(RUN_RESUMED)
       └─ event.set()  ← 唤醒 _wait_for_resume / _handle_pause
```

---

## 三、超时链路

```
SchedulerConfig.pause_timeout_ms = 300_000（默认 5 分钟）
  └─ BaseScheduler.__init__ → self.config
       ├─ _handle_pause()    scheduler.py:134  → 外部暂停，超时不 fail
       └─ _wait_for_resume() scheduler.py:407  → 确认等待，超时 fail
```

`_wait_for_resume` (scheduler.py:403-414):
```python
async def _wait_for_resume(self, run_id: str) -> None:
    event = self._pause_events.setdefault(run_id, asyncio.Event())
    event.clear()
    try:
        await asyncio.wait_for(
            event.wait(),
            timeout=self.config.pause_timeout_ms / 1000.0  # ← 唯一超时控制
        )
    except asyncio.TimeoutError:
        await self._fail(run_id, "Confirmation timed out")  # ← 写 RUN_FAILED
    finally:
        event.clear()
```

三条调用路径在 `_wait_for_resume` 返回后都检查 `state.status in (FAILED, COMPLETED)` 来终止。

---

## 四、问题清单

### 🔴 P0 — 运行时数据竞态

| # | 文件 | 行 | 问题 | 影响 | 状态 |
|---|------|-----|------|------|------|
| CT-1 | `scheduler.py` | 403-414, 268-276 | **`_wait_for_resume` 超时与并发 `resume()` 时序交错** — 超时触发后 `self._fail()` 写 RUN_FAILED，但 confirm 端点同时调用 `resume()` 写 RUN_RESUMED。两事件以不确定顺序进入 Event Store。若 `RUN_RESUMED` 排在最后，fold 结果为 `RUNNING`，调度器继续执行——超时保证被打破 | **数据竞态 → 超时失效** | ✅ 已修复：`_resume_lock` (asyncio.Lock) 串行化 `_fail()` 和 `resume()` 的读-判断-写临界区，消除 TOCTOU |
| CT-2 | `scheduler.py` | 131, 404 | **`_pause_events[run_id]` 被 `_handle_pause` 和 `_wait_for_resume` 共享** — 外部暂停（POST /pause）和确认暂停使用同一个 `asyncio.Event`。一个 `resume()` 可能同时唤醒两个等待，或一个暂停的 `event.clear()` 清除另一个等待的 event | **逻辑串扰** | ❌ |
| CT-3 | `scheduler.py` | 270-276 | **`resume()` 写 RUN_RESUMED 在 `event.set()` 之前** — 如果 `append_event` 后、`event.set()` 前发生 cancel，RUN_RESUMED 已持久化但等待者永远不会被唤醒 | **事件流损坏 → RUNNING 但卡死** | ❌ |
| CT-9 | `harness.log` run `9fb980a7` | seq=5~11 | **`retry_step` 循环中 executor 找不到 ConfirmationReceived** — 用户点击「同意」两次（17:31:06、17:31:46），seq=7 和 seq=9 的 RunResumed 已写入，但 ConfirmationReceived 在事件流中缺失。executor 每次 retry 都返回 CONFIRMATION_NEEDED，形成死循环直到 5 分钟超时。**根因：`confirm` 端点无幂等键，并发写入无 UNIQUE 约束保护，且 API 层 read-check-write 存在 TOCTOU** | **确认死循环 → 用户确认无效** | ✅ 已修复：`/confirm` 传入 `idempotency_key=f"confirm_{confirmation_id}"`，Event Store UNIQUE 约束保证幂等 |

### 🟠 P1 — 功能缺陷

| # | 文件 | 行 | 问题 | 影响 | 状态 |
|---|------|-----|------|------|------|
| CT-4 | `scheduler.py` | 342-376 | **串行路径 `_run_tool_call` 的 CONFIRMATION_NEEDED while True 循环无退出上限** — 如果 executor 持续返回 `CONFIRMATION_NEEDED`（例如 guardrail 规则变化、确认被拒绝后重试仍拒绝），循环永不退出 | **无限循环** | ❌ |
| CT-5 | `scheduler.py` | 762-803, 911-958 | **DAG 路径 PlanSuspended handler 同样无循环退出上限** — 与 CT-4 同理 | **无限循环** | ❌ |
| CT-6 | `scheduler.py` | 403-414 | `_wait_for_resume` 超时后调用 `self._fail()`，但 `_fail()` 不取消正在运行的 asyncio.Task。工具执行可能仍在进行中，TOOL_COMPLETED 事件在 RUN_FAILED 之后到达，fold 结果不确定 | **状态不确定** | ❌ |

### 🟡 P2 — 设计缺陷

| # | 文件 | 行 | 问题 | 影响 | 状态 |
|---|------|-----|------|------|------|
| CT-7 | `scheduler.py` | 129-141 vs 403-414 | **`_handle_pause`（外部暂停）超时不 fail，`_wait_for_resume`（确认等待）超时 fail** — 行为差异未文档化，两个方法共享 `pause_timeout_ms` 但超时语义不同 | **可维护性** | ❌ |
| CT-8 | `scheduler.py` | `SchedulerConfig` | `pause_timeout_ms` 同时控制外部暂停和确认暂停的超时，无法独立配置 | **灵活性不足** | ❌ |

---

## 五、严重时序场景

### 场景 A：超时 → 确认交错（CT-1 具体化）

```
T+0:  _wait_for_resume 超时（asyncio.TimeoutError 触发）
T+1:  confirm 端点写 CONFIRMATION_RECEIVED（异步追加）
T+2:  resume() 写 RUN_RESUMED + event.set()（无人等待，信号丢失）
T+3:  _wait_for_resume finally: event.clear()
T+4:  _wait_for_resume 调用 self._fail() 写 RUN_FAILED（异步追加）
T+5:  调用者 refresh_state → 读出事件流：
      [..., RUN_PAUSED, CONFIRMATION_RECEIVED, RUN_RESUMED, RUN_FAILED]
      → RUN_FAILED 是最后一个 → fold = FAILED（正确，但确认被浪费）
```

### 场景 B：更糟的排序（✅ 已由 `_resume_lock` 修复）

```
T+0:  _wait_for_resume 超时
T+1:  self._fail() 追加 RUN_FAILED（未 flush）
T+2:  confirm 端点追加 CONFIRMATION_RECEIVED
T+3:  resume() 追加 RUN_RESUMED + event.set()
T+4:  _wait_for_resume 返回，调用者 refresh_state
T+5:  读出事件流：
      [..., RUN_PAUSED, RUN_FAILED, CONFIRMATION_RECEIVED, RUN_RESUMED]
      → RUN_RESUMED 是最后一个 → fold = RUNNING！
      → 调度器继续执行，超时从未发生过
```

**修复后**：`_resume_lock` 保证 `_fail()` 和 `resume()` 不会同时执行。若 `_fail()` 先持锁 → `RUN_FAILED` 达成 → `resume()` 阻塞直到 `RUN_FAILED` 可见 → guard 拦截。若 `resume()` 先持锁 → `RUN_RESUMED` 写入 → `_fail()` 随后写 `RUN_FAILED` → fold 结果仍为 `FAILED`（`RUN_FAILED` 是最后一个）。两种路径下最终状态正确。

---

## 六、生产日志实证（2026-06-11 run 9fb980a7）

### 完整事件流

```
seq=1: RunStarted             17:29:14
seq=2: AgentThought           17:29:20
seq=3: PlanCreated            17:29:20
seq=4: DagStepStarted         17:29:20
seq=5: ConfirmationRequested  17:29:20  ← 首次触发确认
seq=6: RunPaused              17:29:20  ← 暂停等待
--- 用户第一次点击"同意"（17:31:06，106s 后）---
seq=7: RunResumed             17:31:06  ← resume() 被调用
       [ConfirmationReceived 缺失！未在事件流中出现]
       retry → executor 仍然返回 CONFIRMATION_NEEDED
seq=8: RunPaused              17:31:06  ← 再次暂停
--- 用户第二次点击"同意"（17:31:46，40s 后）---
seq=9: RunResumed             17:31:46  ← resume() 再次被调用
       [ConfirmationReceived 缺失！]
       retry → executor 仍然返回 CONFIRMATION_NEEDED
seq=10: RunPaused             17:31:46  ← 第三次暂停
--- 5 分钟超时 ---
seq=11: RunFailed             17:36:46  ← Confirmation timed out
```

### 关键观测

1. `ConfirmationReceived` **从未出现在事件流中** —— 对比正常 run（如 `3be6709d` seq=7）会显示 `Written event @ seq=N: ConfirmationReceived`
2. 但 `RunResumed` 出现了两次（seq=7, seq=9），说明 `resume()` 被调用了 —— `confirm` 端点通过了幂等检查并调用了 `scheduler.resume()`
3. executor 每次 retry 都找不到 ConfirmationReceived，陷入死循环
4. 工具（`file_op delete`）的 `destructive` guardrail 每次 retry 都重新触发确认

### 根因

`confirm` 端点无 `idempotency_key` 传递到 `append_event`，API 层的 read-check-write 去重存在 TOCTOU 竞态：

| 事件 | 时间 |
|------|------|
| 请求 A 查表 → 无 ConfirmationReceived | T+0 |
| 请求 B 查表 → 无 ConfirmationReceived | T+1 |
| 请求 A 写 ConfirmationReceived → 成功 | T+2 |
| 请求 B 写 ConfirmationReceived → 也成功（无 UNIQUE 约束拦截） | T+3 |
| 后端的 `executor._find_confirmation_received` 按 `idempotency_key` 查找，查到两条 → 行为不确定 | T+4 |

**修复**：`/confirm` 端点传入 `idempotency_key=f"confirm_{confirmation_id}"`，Event Store 的 UNIQUE 索引保证同样确认只写成功一次。第二个请求撞约束直接失败，无需应用层判断。

---

## 七、修复建议

| # | 方案 | 涉及 | 状态 |
|---|------|------|------|
| F1 | `_resume_lock` (asyncio.Lock) 串行化 `_fail()` 和 `resume()` 的读-判断-写临界区，消除 TOCTOU | CT-1 | ✅ 已实现 |
| F2 | 确认暂停和外部暂停使用独立的 `asyncio.Event`（例如 `_confirm_events` vs `_pause_events`） | CT-2 | 🔜 待做 |
| F3 | CONFIRMATION_NEEDED 循环加入迭代上限（例如 `max_confirm_retries = 10`），超上限后写 RUN_FAILED | CT-4, CT-5 | 🔜 待做 |
| F4 | `_fail()` 执行时连带 cancel 正在运行的 task | CT-6 | 🔜 待做 |
| F5 | `SchedulerConfig` 拆分为 `confirm_timeout_ms` 和 `pause_timeout_ms`，允许独立配置 | CT-8 | 🔜 待做 |
| F6 | retry_step 改用原 confirmation_id 直接校验 | CT-9 | 🔜 待做 |
| F7 | `/confirm` 端点传递 `idempotency_key`，利用 Event Store UNIQUE 约束保证幂等 | CT-9 | ✅ 已实现 |
