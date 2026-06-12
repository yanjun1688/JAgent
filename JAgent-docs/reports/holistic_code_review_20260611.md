# 全量 Code Review 报告 — 2026-06-11

> 审查范围: ~34 文件变动，涵盖 Phase 2 CONFIRMATION_NEEDED、V0.7 变量替换、V0.6.1 Feedback Redesign、上下文压缩增强、调度器重构
> 审查方式: 代码审查 + 日志审查（3954 行 `harness.log`）
> 日志覆盖: 26 个 Run，488 事件，65 次工具调用，7 次失败，5 次 Guardrail 触发

---

## P0 — 破坏性变更（影响运行时行为）

### P0-0: `$s1.uuid.txt` 变量替换正则贪婪匹配路径

| 字段 | 值 |
|------|-----|
| **位置** | `harness/core/dag_executor.py` — `_substitute_vars` 正则 |
| **发现方式** | 日志审查 line 2222 |
| **影响域** | 变量替换层 |

**日志证据**:
```
[var] path 'uuid.txt' not found in variable 's1' (stopped at 'txt')
```

**根因**: 正则 `r'\$(\w+)(?:\.([\w.]+))?'` 匹配 `$s1.uuid.txt` 时，将整个 `.uuid.txt` 作为路径捕获（`path = "uuid.txt"`）。路径遍历到第二段 `txt` 时失败。`_deep_resolve` 的 `found is not None` 检查在 `for` 循环外部——遍历中途 `return m.group(0)` 跳过了它。

**触发条件**: 任何在变量引用后紧跟字面文本的场景（如 `$s1.uuid.txt`、`$s1.name_report`）。

**影响**: 变量不解析，作为纯字符串传递。LLM 生成的 plan 中这种模式很常见（如 `/tmp/$s1.uuid.txt`）。

**修复方向**:
1. 将 `_deep_resolve` 增强为在路径遍历中途失败的段也能尝试搜索剩余段
2. 限制正则只捕获明确路径段，不吞噬字面文件后缀

---

## P1 — 逻辑缺陷（特定场景产生错误行为）

### P1-0: Monitor 读错 GuardrailTriggered 的字段

| 字段 | 值 |
|------|-----|
| **位置** | `harness/monitoring/run_monitor.py:94` |
| **发现方式** | 日志审查 line 2366 + 代码审查 events.py:77-81 |
| **影响域** | 监控反馈 |

**日志证据**:
```
Anomaly threshold hit ... error_type= consecutive=3 event_type=GuardrailTriggered
```
`error_type=` 后面为空。

**根因**: `GuardrailTriggeredPayload`（events.py:77）只有 `reason` 字段没有 `error` 字段：
```python
class GuardrailTriggeredPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    guardrail_id: str
    reason: str         # ← 字段名叫 reason
```

但 `run_monitor.py:94` 读的是 `error`：
```python
error = event.payload.get("error", "")  # ← GuardrailTriggered 没有 "error"
```

所以 `error` 永远为空串，`_extract_error_type("")` 也返回空串。

**影响**: 
- 注入的 FeedbackInjected 的 `error_type` 为空
- P1-3 去重 key `(ep_key, "")` 让所有 GuardrailTriggered 的同端点反馈互相覆盖
- `dominant_error` 也是空串，feedback_text 有语病（"failed 3 consecutive times: ''"）

**修复方向**: 
- 方案 A: `run_monitor.py:94` 改为 `event.payload.get("error") or event.payload.get("reason", "")`
- 方案 B: 统一 GuardrailTriggered 的 payload，加 `error` 字段（破坏性大，影响 fold 等其他消费者）

---

## P2 — 设计缺陷（功能正常但扩展或异常路径有隐患）

### P2-0: 幂等性检查竞态导致沙箱无意义执行

| 字段 | 值 |
|------|-----|
| **位置** | `harness/tools/executor.py` step 2 vs step 7 |
| **发现方式** | 日志审查 lines 1437-1448 |
| **影响域** | 性能，并发安全 |

**日志证据**:
```
sandbox Completed in 390ms (retries=0)
Idempotency cache hit: ToolCompleted @ seq=10
sandbox Completed in 250ms (retries=0)
Idempotency cache hit: ToolCompleted @ seq=10
```

**根因**: 幂等性检查在 step 2（执行之前）进行。s1、s2、s3 并行执行相同请求（同 url+method），step 2 检查时 s1 尚未写入 TOOL_COMPLETED → 全部放行。s1 完成写入后，s2/s3 写入时检测到 idempotency key 重复 → 返回缓存结果，丢弃已执行的沙箱结果。

**影响**: 沙箱执行了但结果被丢弃。并发相同 key 越多浪费越多。

**修复方向**: 在 executor 的写入路径（step 7 之后）加原子化检测，或使用 DB 级唯一约束。

---

### P2-1: 无确认仅 resume 导致无限 pause 循环

| 字段 | 值 |
|------|-----|
| **位置** | `harness/core/scheduler.py` PlanSuspended handler |
| **发现方式** | 日志审查 lines 1000-1027 |
| **影响域** | UX，确认流程 |

**日志证据**:
```
seq=6:  RunPaused
seq=7:  RunResumed       ← 用户只点了"继续"没点"确认"
      → retry → still CONFIRMATION_NEEDED
seq=8:  RunPaused        ← 又暂停了
seq=9:  RunResumed       ← 用户又点"继续"
      → retry → still CONFIRMATION_NEEDED
seq=10: RunPaused        ← 又暂停了
seq=11: RunFailed        ← 300s 超时
```

**根因**: `_wait_for_resume` 通过 `asyncio.Event.wait()` 被 resume 唤醒后返回。但 executor 重新检查确认状态时发现 CONFIRMATION_RECEIVED 不存在（用户只 resume 没确认）→ 返回 CONFIRMATION_NEEDED。while True 循环继续 → 写 RUN_PAUSED → 再次等待。没有重试次数限制和失败兜底。

**影响**: 用户不知道"先确认再继续"的流程，陷入无限循环直到超时。

**修复方向**:
- 方案 A: 在 while True 循环内加重试上限，超限后直接标记为 FAILED
- 方案 B: resume 时检测 pending confirmation，如果没有 CONFIRMATION_RECEIVED 则自动拒绝

---

### P2-2: 确认拒绝后有多余的 RUN_RESUMED

| 字段 | 值 |
|------|-----|
| **位置** | 待排查（executor 拒绝路径或 scheduler resume 路径） |
| **发现方式** | 日志审查 lines 1694-1696 |
| **影响域** | 事件流一致性 |

**日志证据**:
```
seq=7:  ConfirmationReceived
seq=8:  RunResumed        ← 来自 scheduler.resume()
      → retry → Operator declined
seq=9:  RunResumed        ← 来源不明
seq=10: ToolFailed
```

**根因**: 第一个 RUN_RESUMED（seq=8）来自用户调用的 resume。retry 后确认被拒绝，但返回前某个路径产生了第二个 RUN_RESUMED（seq=9）。

**影响**: 事件流语义矛盾——操作被拒绝但写着 RunResumed。

**待查明**: 需要排查 executor 的 `_check_confirmation` 返回路径和 scheduler PlanSuspended handler 的 while True 循环，看谁在拒绝后额外写了一次 RUN_RESUMED。

---

## P3 — 代码质量（无运行时影响）

### P3-0: Monitor cleanup 日志 pending_calls 始终为 0（已修复）

| 字段 | 值 |
|------|-----|
| **位置** | `harness/monitoring/run_monitor.py:405,418` |
| **状态** | ✅ 已修复 |

原始 review 发现的 bug。修复后日志正常显示 pending_calls 实际值（line 3945: `Cleaned up run add97085 ... pending_calls=3`）。

---

### P3-1: Guardrail 检查不在 semaphore 内

| 字段 | 值 |
|------|-----|
| **位置** | `harness/core/dag_executor.py` |
| **发现方式** | 日志审查 lines 377-496 |

30 个 step 在同一层执行时，guardrail 检查全部并发（不在 max_parallel semaphore 内），沙箱执行才受限制。当前无问题，只是架构观察。

---

## 修复优先级建议

| 优先级 | Bug | 影响范围 | 修复成本 |
|--------|-----|----------|----------|
| P0 | P0-0: `$s1.uuid.txt` 变量替换正则 | 所有含字面后缀的变量引用 | 中等 |
| P1 | P1-0: Monitor 读错 GuardrailTriggered 字段 | 监控反馈准确性 | 低（加 fallback） |
| P2 | P2-2: 多余 RUN_RESUMED | 事件流语义 | 需要先排查 |
| P2 | P2-1: resume 不确认无限循环 | UX 和防死循环 | 中等 |
| P2 | P2-0: 幂等性竞态 | 沙箱资源浪费 | 低 |
