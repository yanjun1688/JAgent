# Harness 监控反馈机制改造方案

> **当前阶段**: 设计文档
> **关联里程碑**: V0.6.1 — 反馈机制增强
> **文档版本**: v1.2
> **最后更新**: 2026-06-08

---

## 1. 背景

### 1.1 问题诊断

从 `test-logs.md` 第 323-325 行可见：

```
13:02:05 [MONITOR] Anomaly: 3 consecutive tool failures (threshold=3), injecting high-priority feedback
13:02:05 [MONITOR] Injected [high] feedback: Warning: 3 consecutive tool failures detected.
             Consider checking input parameters or terminating the task.
```

实际场景：3 个 `browser.navigate` 调用全部因为 Windows 上 Playwright 的 `NotImplementedError` 失败。但反馈文本从不说"browser 挂了"，只说"检查参数或终止"。

### 1.2 八个独立问题

| # | 问题 | 严重性 | 影响 |
|---|------|--------|------|
| P0—反馈内容 | 反馈内容宽泛，无具体信息 | 致命 | 没说哪个工具 (`browser`)、什么错误 (`NotImplementedError`)、模式（同工具同错） |
| P0—建议缺失 | 没有建设性建议 | 致命 | 只说"检查参数或终止"，没说"browser 不可用，改用 http_request" |
| P0—反馈链路 | 反馈流不到 Planner revise | 致命 | `Planner.revise()` 用 `_REVISE_PROMPT`，这个模板没有 `{feedback}` 占位占 |
| P0—Schema 治理 | **Schema 定义、prompt、校验三处脱节** | 致命 | `_REVISE_PROMPT` 无 schema 示例 → LLM 从 tool descriptions 推断 → 输出 `action`/`url` 在 step 顶级 → `_parse_plan` 仅返回 `None` → retry 消息零诊断 → 3 次全失败（test-logs 第 396/462 行）。优先级高于反馈修复 |
| P1—过期 | 反馈永不过期 | 严重 | 一次反馈永久存在，后面成功了还留着，混淆 Agent |
| P1—防重粒度过粗 | `_failure_feedback_sent` 以 `run_id` 为 key，全 run 只能发一次反馈 | 严重 | 故障模式变化后（browser 失败→http_request 也开始失败），不会触发新反馈 |
| P1—intent 丢失 | revise prompt 的 `## Original User Intent\n(unknown)` | 严重 | test-logs 第 340 行确认，Planner revise 不知道原始意图 |
| P2—retry 信息不足 | retry 消息只提示"输出不是有效 JSON"，不告知具体 schema 字段错误 | 一般 | LLM 持续输出相同结构的错误 JSON，3 次全部 Parse failed |

### 1.3 代码根因

```
┌─ FeedbackInjectedPayload（events.py:142-144）
│  只有 feedback_text + priority，没有 tool/error/suggestion/expires/source 等结构
│  priority 缺 "low" 级别（events.py:144, Literal["high", "medium"]）
│
├─ RunMonitor（run_monitor.py:69-81）
│  只计数"连续失败次数"(全局 counter)，不记录"哪个工具"、"什么错误"
│  _failure_feedback_sent 以 run_id 为 key（run_monitor.py:46）— 全 run 只发一次反馈
│  TOOL_FAILED 和 GUARDRAIL_TRIGGERED 两分支各自独立检测，无统一模式识别
│  生成的是固定字符串模板，无上下文感知
│
├─ Scheduler._get_feedback_text（scheduler.py:130-136）
│  只是 "\n".join(feedback_text)，优先级被丢弃
│
├─ Planner.revise + Planner.plan（planner.py:67-82, 200, 232）
│  _REVISE_PROMPT 和 _PLAN_PROMPT 都没有 {feedback} 占位占
│  revise()/plan() 方法签名也没有 feedback 参数
│
├─ PlanningExecutorScheduler 不传 feedback（scheduler.py:811, 855, 721, 749, 928）
│  所有 revise/plan 调用点都不传 feedback_text
│  即使 Planner 接收了 feedback，Scheduler 也不传
│
├─ Schema 定义、prompt、校验三处脱节（planner.py:22-418）
│  1. _REVISE_PROMPT（行67-82）没有任何 schema 示例，只说 "same as before"
│  2. _parse_plan（行374-418）是手写解析，失败只 return None
│  3. retry 消息（行84-86）只说"不是有效 JSON"
│  串联效应：LLM 从 tool descriptions 推断结构 → action/url 放 step 顶层
│  → _parse_plan 不认 → return None → retry 无诊断 → 同错重犯 → RunFailed
│
├─ revise intent 丢失（planner.py:239）
│  plan.intent 默认为空字串 → _REVISE_PROMPT 中显示 "(unknown)"
│  test-logs 第 340 行确认
│
└─ 无 Operator 手动反馈通道
    没有 API 端点让人在运行中给 Agent 发消息
```

---

## 2. 设计方案

### 2.1 设计原则

1. **最小改动** — 不改现有事件流架构，不改 `fold_events()`，不改 `EventType` 枚举
2. **向后兼容** — 现有 `feedback_text` 字段保留，老反馈照常工作。所有新字段都是 `Optional`
3. **受信边界** — Monitor 仍然是受信组件，反馈仍然走 EventStore → fold → Scheduler 路径
4. **事件溯源一致性** — Monitor 内存状态（如 `_failure_feedback_sent`）不再用于防重，改由 EventStore 推导
5. **确定性 ID** — feedback_id 仅基于事件内容 hash，不依赖时间，保证重放/恢复/多实例一致性

### 2.2 改动一览

| 文件 | 改动类型 | 行数 |
|------|----------|------|
| `harness/models/events.py` | 增强 `FeedbackInjectedPayload`，加 8 个可选字段 + `FeedbackCategory` + `FeedbackSource` 枚举 + `priority` 加 `"low"` | ~35 |
| `harness/monitoring/run_monitor.py` | 重构检测逻辑：per-tool 追踪 + 错误模式识别 + GUARDRAIL_TRIGGERED 统一追踪 + 建议生成 + 分辨率信号 + EventStore 推导防重 + 确定性 hash + expires_at_seq 三级策略 + cleanup 同步 | ~130 |
| `harness/core/scheduler.py` | 改进反馈渲染格式 + `_get_feedback_text()` 新增 `for_revise` 参数（仅 high） + 过滤过期/被解决的反馈 + source 不同渲染 | ~60 |
| `harness/core/planner.py` | `_REVISE_PROMPT` 加 `{feedback_section}` + `_PLAN_PROMPT` 加 `{feedback_section}` + `revise()`/`plan()` 加 `feedback` 参数 + `_build_feedback_section()` 辅助方法 + **JSON Schema 统一驱动(_build_step_schema_text + _validate_step + _parse_plan 改为 jsonschema.validate)** + **retry 消息带具体 schema 错误** + **两 prompt 共用同一 schema 描述** + **intent 传递修复** | ~100 |
| `harness/core/scheduler.py` (ExecutionScheduler) | 所有 revise/plan 调用点传 feedback（影响 5 处） | ~15 |
| `harness/api/routes.py` | 新增 `POST /api/v1/runs/{run_id}/feedback` + Operator 反馈 `feedback_id` 计算 | ~35 |
| `tests/test_monitoring.py` | 新增 ~14 个测试用例（含 JSON Schema 校验 4 个、反馈链路 4 个、schema 统一性 2 个、多模式反馈 1 个、Operator feedback_id 等） | ~200 |

**总计：约 535 行代码（含测试）**

---

## 3. 详细设计

### 3.1 结构化 Payload

**文件**: `harness/models/events.py`

```python
class FeedbackCategory(str, Enum):
    TOOL_FAILURE = "tool_failure"
    TOKEN_WARNING = "token_warning"
    REPEATED_CALL = "repeated_call"
    GUARDRAIL_TRIGGERED = "guardrail_triggered"
    OPERATOR_ADVICE = "operator_advice"
    CONDITION_RESOLVED = "condition_resolved"

class FeedbackSource(str, Enum):
    MONITOR = "monitor"     # 自动检测注入
    OPERATOR = "operator"   # 人工 API 注入

class FeedbackInjectedPayload(BaseModel):
    feedback_id: str = ""
    source: FeedbackSource = FeedbackSource.MONITOR
    category: FeedbackCategory = FeedbackCategory.OPERATOR_ADVICE
    feedback_text: str
    priority: Literal["high", "medium", "low"] = "medium"
    affected_tool: str | None = None          # 哪个工具出问题
    error_type: str | None = None             # 异常类名（NotImplementedError）
    error_detail: str | None = None           # 完整错误消息（前 200 字）
    suggestion: str | None = None             # 具体替代建议
    expires_at_seq: int | None = None         # 在此 seq 后自动过期
    resolves_feedback_id: str | None = None   # 解决了哪条旧反馈
```

**关键设计**: 所有新字段都是 `Optional`。老数据零兼容成本。

### 3.2 feedback_id — 确定性 hash

**不依赖时间，不依赖随机数**。仅基于事件内容推导，保证重放、恢复、多实例产生相同 ID。

```python
def _compute_feedback_id(
    run_id: str, category: FeedbackCategory,
    tool: str | None, error: str | None,
) -> str:
    raw = f"{run_id}:{category.value}:{tool or '?'}:{error or '?'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

**去重机制**（防 `on_append` 回调重入）：不做前端去重。EventStore 的幂等键机制自然处理重复写入。

### 3.3 expires_at_seq 三级策略

| Priority | 含义 | expires_at_seq = current_seq + |
|----------|------|-------------------------------|
| high | 严重问题需修复 | **+50** (~10-12 次迭代) |
| medium | 建议性预警 | **+30** (~6-8 次迭代) |
| low | 信息 / 已解决 | **+10** (~2-3 次迭代) |

> **未来扩展**: 双条件过期（seq + time）——当 seq 增长缓慢时可增加 `expires_at` 时间戳条件。
> 当前阶段 seq-only 足够，因为 all 工具调用都会产生 seq。

### 3.4 RunMonitor 增强

**文件**: `harness/monitoring/run_monitor.py`

#### 3.4.1 新增追踪状态

```python
self._failures_per_tool: dict[str, dict[str, int]] = {}             # run_id → {tool_name: fail_count}
self._failure_error_map: dict[str, dict[str, dict[str, int]]] = {}  # run_id → {tool_name: {error_type: count}}
self._captured_failure_tool: dict[str, str | None] = {}             # run_id → 触发反馈的工具（用于分辨率判断）
```

> 注意：这些是 Monitor 进程内存态，Monitor 重启后重建。依赖这些状态的失效保护见 3.4.3。
>
> **设计决策**: `_consecutive_failures` 保持全局计数器而非 per-tool。触发条件是"连续 N 次任意失败"，但 `_check_and_inject_feedback` 接收具体的触发工具+错误。防重用 `(category, tool, error_type)` 三元组而非 dominant 推导，允许多模式反馈（先 browser 失败后 http 失败，两次独立触发）。

#### 3.4.2 统一模式识别

TOOL_FAILED 和 GUARDRAIL_TRIGGERED 统一走同一套逻辑：

```
TOOL_FAILED ─┐
              ├─ 全局 consecutive += 1
              ├─ per_tool[rid][tool] += 1
              ├─ per_error[rid][tool][error_type] += 1
GUARDRAIL_    │
TRIGGERED ────┘
              │
              └─ count >= 3 → _check_and_inject_feedback(rid, tool, error_type, error)
                  ├─ 纯度检查：该工具出错次数中 dominant error ≥80% 才给建议
                  ├─ 查询 EventStore：同 (category, tool, error_type) 已有 active 反馈？
                  ├─ 有 → 跳过（防重）
                  └─ 无 → 写入结构化 FeedbackInjected
```

```python
async def _on_event_impl(self, event: Event) -> None:
    rid = event.run_id

    if event.event_type in (EventType.TOOL_FAILED, EventType.GUARDRAIL_TRIGGERED):
        tool = event.payload.get("tool_name", "?")
        error = event.payload.get("error", "")
        error_type = self._extract_error_type(error)  # 取异常类名，不解析 message
        
        count = self._consecutive_failures.get(rid, 0) + 1
        self._consecutive_failures[rid] = count
        
        per_tool = self._failures_per_tool.setdefault(rid, {})
        per_tool[tool] = per_tool.get(tool, 0) + 1
        
        err_map = self._failure_error_map.setdefault(rid, {}).setdefault(tool, {})
        err_map[error_type] = err_map.get(error_type, 0) + 1
        
        if count >= 3:
            await self._check_and_inject_feedback(rid, tool, error_type, error_detail=error)

def _extract_error_type(self, error_text: str) -> str:
    """提取异常类名，不解析 message。
    
    'NotImplementedError: ...' → 'NotImplementedError'
    'PlaywrightError: Browser closed unexpectedly' → 'PlaywrightError'
    'TimeoutError' → 'TimeoutError'
    """
    return error_text.split(":")[0].strip() if ":" in error_text else error_text.strip()
```

#### 3.4.3 反馈触发方法（直接传工具，不推导 dominant）

```python
async def _check_and_inject_feedback(
    self, rid: str, tool: str, error_type: str,
    error_detail: str = "",
) -> None:
    err_map = self._failure_error_map.get(rid, {}).get(tool, {})
    total_errors = sum(err_map.values())
    
    if total_errors == 0:
        return
    
    # 纯度检查：同工具内 dominant error 占比 ≥80% 才给建议
    dominant_error = max(err_map, key=err_map.get)
    dominant_count = err_map[dominant_error]
    is_mixed = (dominant_count / total_errors) < 0.8
    suggestion = None if is_mixed else self._generate_suggestion(tool, dominant_error)
    
    # 从事件中获取当前 seq（调用方应确保 event.seq 可用）
    current_seq = self._last_seen_seq.get(rid, 0)
    
    # 防重检查：同 (category, tool, error_type) 是否有活跃反馈
    category = FeedbackCategory.TOOL_FAILURE
    if await self._has_active_feedback(rid, category, tool, error_type):
        return
    
    self._captured_failure_tool[rid] = tool
    
    await self._inject_feedback(
        rid, "high",
        feedback_text=f"Tool '{tool}' failed {dominant_count}/{total_errors} times with '{dominant_error}'",
        category=category,
        affected_tool=tool,
        error_type=dominant_error,
        error_detail=error_detail[:200],
        suggestion=suggestion,
        expires_at_seq=current_seq + 50,
    )

async def _has_active_feedback(
    self, rid: str, category: FeedbackCategory,
    tool: str | None, error: str | None,
) -> bool:
    """从 EventStore 折叠状态推导是否已有同类活跃反馈。
    
    替代旧的 _failure_feedback_sent 内存集合，保证 Monitor 重启后不重复注入。
    防重 key = (category, tool, error_type)，支持多模式反馈独立触发。
    """
    events = await self.store.get_events(rid)
    state = fold_events(events)
    for fb in state.feedbacks[-10:]:
        if (fb.category == category
            and fb.affected_tool == tool
            and fb.error_type == error
            and fb.category != FeedbackCategory.CONDITION_RESOLVED
            and (fb.expires_at_seq is None or state.seq <= fb.expires_at_seq)):
            return True
    return False
```

#### 3.4.4 建议生成器

```python
def _generate_suggestion(self, tool: str, error_type: str) -> str | None:
    """基于工具+异常类名生成具体替代建议。
    
    当前硬编码 4 种已知模式，适用于 ~10 个工具内的范围。
    TODO: 当工具超过 10 个时考虑抽象为 FailureAdvisor 注册机制。
    """
    suggestions = {
        ("browser", "NotImplementedError"):
            "The browser tool is unavailable on this platform. Use 'http_request' for web requests.",
        ("browser", "Timeout"):
            "Browser requests are timing out. Use 'http_request' with adjusted timeout.",
        ("http_request", "ConnectTimeout"):
            "HTTP connection timed out. Check network or try a different URL.",
        ("http_request", "InvalidURL"):
            "Invalid URL format. Check and correct the URL before retrying.",
    }
    for (t, e), s in suggestions.items():
        if tool == t and error_type.startswith(e):
            return s
    return None
```

#### 3.4.5 TOOL_COMPLETED 触发分辨率信号

条件：**仅当连续失败数量 ≥3 且本次成功的工具是上次触发反馈的工具时**，才发 CONDITION_RESOLVED。

```python
if event.event_type == EventType.TOOL_COMPLETED:
    was = self._consecutive_failures.get(rid, 0)
    tool = event.payload.get("tool_name", "")
    self._consecutive_failures[rid] = 0
    
    if was >= 3 and tool == self._captured_failure_tool.get(rid):
        # 那个一直失败的工具终于成功了 → 发分辨率信号
        await self._inject_feedback(
            rid, "low",
            feedback_text=f"Tool '{tool}' recovered after {was} consecutive failures",
            category=FeedbackCategory.CONDITION_RESOLVED,
            resolves_feedback_id=previous_fb_id,
            expires_at_seq=current_seq + 10,
        )
```

#### 3.4.6 增强的 `_inject_feedback`

```python
async def _inject_feedback(
    self, run_id, priority, feedback_text, *,
    source=FeedbackSource.MONITOR,
    category=FeedbackCategory.OPERATOR_ADVICE,
    affected_tool=None, error_type=None, error_detail=None,
    suggestion=None, expires_at_seq=None, resolves_feedback_id=None,
) -> str:
    feedback_id = self._compute_feedback_id(run_id, category, affected_tool, error_type)
    
    payload = FeedbackInjectedPayload(
        feedback_id=feedback_id, source=source, category=category,
        feedback_text=feedback_text, priority=priority,
        affected_tool=affected_tool, error_type=error_type,
        error_detail=error_detail, suggestion=suggestion,
        expires_at_seq=expires_at_seq,
        resolves_feedback_id=resolves_feedback_id,
    )
    await self.store.append_event(run_id, EventType.FEEDBACK_INJECTED, payload.model_dump())
    return feedback_id
```

#### 3.4.7 cleanup 简化

`_failure_feedback_sent` 不再需要，cleanup 更新为：

```python
def cleanup(self, run_id: str) -> None:
    self._consecutive_failures.pop(run_id, None)
    self._token_totals.pop(run_id, None)
    self._failure_error_map.pop(run_id, None)
    self._failures_per_tool.pop(run_id, None)
    self._captured_failure_tool.pop(run_id, None)
    # ... 其他原有 cleanup ...
```

### 3.5 反馈渲染 + 过期过滤

**文件**: `harness/core/scheduler.py`

#### 3.5.1 格式化方法（含 source 区分）

```python
def _format_feedback(self, fb: FeedbackInjectedPayload) -> str:
    if fb.category == FeedbackCategory.CONDITION_RESOLVED:
        return f"[RESOLVED] {fb.feedback_text}"
    
    level = "!!" if fb.priority == "high" else "!" if fb.priority == "medium" else ""
    source_tag = "[Operator]" if fb.source == FeedbackSource.OPERATOR else ""
    parts = [f"{level} {source_tag}[{fb.priority.upper()}] {fb.feedback_text}"]
    
    if fb.affected_tool and fb.error_type:
        parts.append(f"   Tool: {fb.affected_tool}  Error: {fb.error_type}")
    if fb.error_detail:
        parts.append(f"   Detail: {fb.error_detail[:120]}")
    if fb.suggestion:
        parts.append(f"   → {fb.suggestion}")
    return "\n".join(parts)
```

#### 3.5.2 过滤 + 渲染（含 revise 模式）

```python
def _get_feedback_text(self, state: RunState, *, for_revise: bool = False) -> str | None:
    """取活跃反馈，渲染为 Agent 可见的结构化文本。
    
    for_revise=True: 仅保留 high 优先级（避免 Planner revise 被噪音淹没）。
    """
    if not self.monitor:
        return None
    
    active = [
        fb for fb in state.feedbacks[-10:]
        if (fb.expires_at_seq is None or state.seq <= fb.expires_at_seq)
        and fb.category != FeedbackCategory.CONDITION_RESOLVED
    ]
    
    # 被解决标记隐藏
    resolved_ids = {
        fb.resolves_feedback_id for fb in state.feedbacks[-10:]
        if fb.category == FeedbackCategory.CONDITION_RESOLVED and fb.resolves_feedback_id
    }
    active = [fb for fb in active if fb.feedback_id not in resolved_ids]
    
    # 优先级排序：operator > monitor high > monitor medium > monitor low
    priority_score = {"high": 3, "medium": 2, "low": 1}
    active.sort(key=lambda fb: (
        1 if fb.source == FeedbackSource.OPERATOR else 0,
        priority_score.get(fb.priority, 0),
    ), reverse=True)
    
    if for_revise:
        active = [fb for fb in active if fb.priority == "high" or fb.source == FeedbackSource.OPERATOR]
    
    if not active:
        return None
    
    rendered = [self._format_feedback(fb) for fb in active[:5]]
    return (
        "## Monitoring Feedback\n"
        + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n".join(rendered)
    )
```

**Agent 看到的效果**：

```
## Monitoring Feedback
!! [Operator][HIGH] browser 不可用，全部改用 http_request
   → 这是人工指示，请优先遵守
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
!! [HIGH] Tool 'browser' failed 3/3 times with 'NotImplementedError'
   Tool: browser  Error: NotImplementedError
   Detail: Browser action 'navigate' failed: NotImplementedError:
   → The browser tool is unavailable. Use 'http_request' for web requests.
```

### 3.6 Planner 反馈注入

**文件**: `harness/core/planner.py`

#### 3.6.1 `_REVISE_PROMPT` 和 `_PLAN_PROMPT` 加 `{feedback_section}`

两个 prompt 模板都增加同一段注入：

```python
_REVISE_PROMPT = """You are a task planner reviewing execution results.
...
{system_state}
{feedback_section}
...
"""

_PLAN_PROMPT = """You are a task planner. Given a user intent and available tools,
create a step-by-step plan in JSON format.
...
{feedback_section}
## User Intent
{intent}
"""
```

#### 3.6.2 `revise()` 和 `plan()` 方法新增 `feedback` 参数

```python
async def revise(self, plan, results, system_state, feedback: str | None = None) -> DagPlan | None:
    feedback_section = self._build_feedback_section(feedback)
    prompt = _REVISE_PROMPT.format(
        intent=plan.intent or intent_fallback,  # ← 修复 (unknown) 问题
        system_state=system_state,
        feedback_section=feedback_section,
        tool_descriptions=self._build_tool_descriptions(),
    )
    ...

async def plan(self, intent, state=None, feedback: str | None = None) -> DagPlan | None:
    feedback_section = self._build_feedback_section(feedback)
    prompt = _PLAN_PROMPT.format(
        intent=intent,
        tool_descriptions=self._build_tool_descriptions(),
        feedback_section=feedback_section,
    )
    ...

def _build_feedback_section(self, feedback: str | None) -> str:
    if not feedback:
        return ""
    return (
        f"## System Monitoring Feedback\n"
        f"{feedback}\n"
        f"Take this feedback into account when planning the next steps.\n"
    )
```

> **intent 修复**: `revise()` 当前使用 `plan.intent[:200] if plan.intent else "(unknown)"`。因为 `_parse_plan` 不解析 LLM 输出的 `intent` 字段，plan.intent 一直为空。修复方案：revise() 额外接收 `intent_fallback: str` 参数，当 `plan.intent` 为空时使用。

#### 3.6.3 Scheduler 传入反馈

`PlanningExecutorScheduler` 中 revise 和 plan 的调用点都传入 feedback。

```python
# revise 路径（_execute_static_plan 层失败后）
state = await self._refresh_state(run_id)
feedback_text = self._get_feedback_text(state, for_revise=True)
revised = await self.planner.revise(plan, results, sys_state, feedback=feedback_text)

# revise 路径（全部步骤成功后的 revise check）
feedback_text = self._get_feedback_text(state, for_revise=True)
revised = await self.planner.revise(plan, results, sys_state, feedback=feedback_text)

# revise 路径（_execute_dynamic_plan 步骤失败后）
feedback_text = self._get_feedback_text(state, for_revise=True)
revised = await self.planner.revise(plan, results, sys_state, feedback=feedback_text)

# plan 路径（_get_or_fallback 中首次规划时）
feedback_text = self._get_feedback_text(state, for_revise=True)
plan = await self.planner.plan(intent, state, feedback=feedback_text)
```

> 影响 `scheduler.py` 5 个调用点：第 811 行（层失败 revise）、第 855 行（全部完成 revise）、第 721 行（动态规划步骤失败 revise）、第 749 行（动态规划步骤成功 revise）、第 928 行（首次 plan）。

### 3.7 Operator 手动反馈 API

**文件**: `harness/api/routes.py`

```python
@router.post("/api/v1/runs/{run_id}/feedback")
async def operator_feedback(
    run_id: str,
    body: OperatorFeedbackRequest,
    hapi: HarnessAPI = Depends(get_hapi),
):
    """Operator 在运行中注入手动反馈，走 EventStore→fold→Scheduler 路径。"""
    payload = FeedbackInjectedPayload(
        source=FeedbackSource.OPERATOR,
        category=FeedbackCategory.OPERATOR_ADVICE,
        feedback_text=body.text,
        priority=body.priority,
        suggestion=body.suggestion,
        expires_at_seq=body.expires_in_seqs,
    )
    await hapi.store.append_event(
        run_id, EventType.FEEDBACK_INJECTED, payload.model_dump(),
    )
    return {"status": "ok", "feedback_id": payload.feedback_id}

class OperatorFeedbackRequest(BaseModel):
    text: str = Field(..., max_length=500)
    priority: Literal["high", "medium", "low"] = "medium"
    suggestion: str | None = Field(None, max_length=300)
    expires_in_seqs: int | None = Field(None, ge=1, le=500)
```

### 3.8 Schema 统一治理：JSON Schema 驱动 Prompt + 校验 + Retry

**文件**: `harness/core/planner.py`

#### 3.8.1 问题定位

`_parse_plan` 不是独立 bug，问题链条：

```
_REVISE_PROMPT 没有 schema 示例
    → LLM 从 tool descriptions 的字段名推断结构
    → 把 action/url 放在 step 顶层（而非 input 内）
    → _parse_plan 手写解析 → input=None → return None
    → retry 消息只说"不是有效 JSON"
    → LLM 不知道错在哪里，反复输出相同结构
    → 3 次后 RunFailed
```

**根源**：schema 定义、prompt 生成、输出校验三处各写各的，互相脱节。

#### 3.8.2 设计：JSON Schema 统一驱动

利用项目已有的 `jsonschema` 依赖（`pyproject.toml`，已用于 `guardrails.py`/`executor.py`），将三处统一：

```
DagStep Pydantic Model
    │
    ├─→ 导出 JSON Schema （planner.py 常量）
    │
    ├─→ _PLAN_PROMPT / _REVISE_PROMPT 共用此 schema
    │   生成示例文本（确保两处结构描述一致）
    │
    └─→ _parse_plan 用 jsonschema.validate()
        校验每个 step → ValidationError
              ↓
        提取 error.message + error.path
              ↓
        注入 retry 消息 + 指示修复方向
```

#### 3.8.3 具体实现

**Step 1: 从 DagStep 导出 JSON Schema**

```python
from harness.models.plan import DagStep
from pydantic import TypeAdapter

# 导出 JSON Schema（项目模式：Pydantic → JSON Schema）
_STEP_SCHEMA = TypeAdapter(DagStep).json_schema()
# 或手写一个精简版（避免 LLM 被不必要字段干扰）：
_STEP_SCHEMA_SIMPLE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique step id, e.g. 's1', 's2'"},
        "tool": {"type": "string", "description": "Tool name from available tools"},
        "input": {
            "type": "object",
            "description": "Parameters for the tool — ALL action/url/query params go HERE, not at step level",
            "additionalProperties": True,
        },
        "depends_on": {
            "type": "array", "items": {"type": "string"},
            "description": "IDs of steps this step depends on (empty if independent)",
        },
        "description": {"type": "string", "description": "What this step does"},
    },
    "required": ["id", "tool", "input"],
    "additionalProperties": False,
}
```

**Step 2: Prompt 共用同一 schema 描述**

`_PLAN_PROMPT` 和 `_REVISE_PROMPT` 使用同一辅助方法生成 schema 说明：

```python
def _build_step_schema_text() -> str:
    """基于 _STEP_SCHEMA_SIMPLE 生成 LLM 可读的格式说明，用于两个 prompt。"""
    return """Each step MUST be a JSON object with exactly these fields:
  - "id" (string, required): unique identifier, e.g. "s1"
  - "tool" (string, required): tool name from the available tools list
  - "input" (object, required): ALL parameters go inside this object.
    NEVER put parameters like 'action', 'url', 'query' at the step level.
    ✅ Correct: {"id": "s1", "tool": "http_request", "input": {"action": "GET", "url": "..."}}
    ❌ Wrong:   {"id": "s1", "tool": "http_request", "action": "GET", "url": "..."}
  - "depends_on" (array of strings, optional): step dependencies for DAG ordering
  - "description" (string, optional): what this step does

No other fields are allowed at the step level.
"""
```

两个 prompt 中统一调用：

```python
_PLAN_PROMPT = f"""...
## Output JSON format
{_build_step_schema_text()}
## Available Tools
{{tool_descriptions}}
## User Intent
{{intent}}
"""

_REVISE_PROMPT = f"""...
## Output JSON format
{_build_step_schema_text()}
## Available Tools
{{tool_descriptions}}
"""
```

**Step 3: `_parse_plan` 改用 `jsonschema.validate()`**

```python
import jsonschema
from jsonschema import ValidationError

def _validate_step(step: dict, step_index: int) -> str | None:
    """验证单个 step 是否符合 schema，返回错误描述（None 表示通过）。"""
    try:
        jsonschema.validate(instance=step, schema=_STEP_SCHEMA_SIMPLE)
    except ValidationError as e:
        # 从 error.path 提取字段名
        bad_field = ".".join(str(p) for p in e.path) if e.path else "structure"
        return (
            f"Step '{step.get('id', f'#{step_index}')}' has an error: "
            f"field '{bad_field}': {e.message}. "
            f"Remember: ALL tool parameters must be inside 'input'."
        )
    return None

def _parse_plan(response: str) -> tuple[DagPlan | None, str]:
    """返回 (plan_or_None, error_reason)"""
    response = response.strip()
    if response.startswith("```"):
        response = response.split("\n", 1)[-1]
        response = response.rsplit("```", 1)[0]
        response = response.strip()

    try:
        data = json.loads(response)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e.msg} at position {e.pos}"

    if not isinstance(data, dict):
        return None, "Top-level value must be a JSON object with a 'steps' array"

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list):
        return None, "Missing or invalid 'steps' array"

    steps = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            return None, f"Step #{i} is not a JSON object"

        # 先做 schema 校验（捕获 action/url 在顶层的错误）
        err = _validate_step(s, i)
        if err:
            return None, err

        # 兼容：input 为空时从 parameters 补
        step_input = s.get("input") or s.get("parameters") or {}

        steps.append(DagStep(
            id=s.get("id", ""),
            tool=s.get("tool", ""),
            input=step_input,
            depends_on=s.get("depends_on", []),
            description=s.get("description", ""),
        ))

    return DagPlan(
        intent=data.get("intent", ""),
        steps=steps,
        dynamic=data.get("dynamic", False),
    ), ""
```

**Step 4: Retry 携带具体 schema 错误**

```python
last_error = ""
for attempt in range(1, total_attempts + 1):
    response = await self.llm.chat(messages, temperature=0.0)
    plan, last_error = self._parse_plan(response)
    if plan is None:
        messages.append({
            "role": "user",
            "content": (
                f"Your previous response had a format error:\n"
                f"{last_error}\n\n"
                f"Please fix this and output ONLY valid JSON.\n"
                f"Remember the required format:\n"
                f"{_build_step_schema_text()}"
            )
        })
        _log.warning("[revise] Parse failed on attempt %d: %s", attempt, last_error)
        continue
```

#### 3.8.4 对比：新方案 vs 旧兼容补丁

| 维度 | 旧方案（收集非保留字段） | 新方案（JSON Schema 驱动） |
|------|--------------------------|---------------------------|
| 本质 | 后门兜底，容忍错误格式 | 从源头减少错误 + 精确反馈 |
| prompt 一致性 | `_REVISE_PROMPT` 仍然靠人工维护示例 | 两 prompt 共用同一自动生成描述 |
| 错误信息 | `return None`，语义丢失 | `ValidationError.message` + `path`，精确到字段 |
| LLM 学习效率 | 不告知错误，下轮重试大概率同错 | 告知具体字段错误，LLM 可针对性修正 |
| 可维护性 | 手写保留字段列表，随着 DagStep 新增字段需要同步 | 从 Pydantic 导出或集中定义，改一处自动同步 |
| 叠加 `response_format` | 不冲突 | 兼容，可扩展 |

> **关于 `response_format: json_schema`（Path C）**：当底层 LLM API 支持时，可以将 `_STEP_SCHEMA_SIMPLE` 作为 `response_format` 传入，由模型层强制输出结构。这层代码已预留兼容接口（TODO_v2.1.md 第 85 行），但不作为主路径。

### 3.9 `PlanningExecutorScheduler` 所有 revise/plan 调用点传 feedback

**文件**: `harness/core/scheduler.py`

当前 5 个调用点全部不传 feedback（`scheduler.py:811, 855, 721, 749, 928`）。修复方式：

```python
# 每一处调用前获取 state 时，同时获取 feedback_text
state = await self._refresh_state(run_id)
feedback_text = self._get_feedback_text(state, for_revise=True)

# 然后传入
revised = await self.planner.revise(plan, results, sys_state, feedback=feedback_text)
```

> 注意：`_get_feedback_text` 在 `BaseScheduler` 中定义（`scheduler.py:130`），`PlanningExecutorScheduler` 继承后可直接使用。

---

## 4. 反馈生命周期

### 4.1 Event Sourcing 天然保证

FeedbackInjected 是 Event，写入 EventStore 后：

- **持久化**: 永久保存，不丢失
- **Checkpoint**: ContextManager checkpoint 包含 feedbacks 列表，restore 后自动恢复
- **Replay**: fold_events() 按 seq 重放，feedback 状态精确还原
- **Monitor 重启**: 内存状态丢失，但 `_has_active_feedback()` 从 EventStore 推导，不会重复注入

### 4.2 主动过期条件

| 条件 | 触发方式 | 结果 |
|------|----------|------|
| seq 超过 expires_at_seq | 折叠时 scheduler 过滤 | 从展示列表移除 |
| 被 CONDITION_RESOLVED 关联 | RunMonitor 自动触发 | 从展示列表隐藏 |
| Operator 手动取消 | 预留（未来可加 cancel API） | 从展示列表移除 |

---

## 5. 数据流对比

### 改造前

```
browser 连续失败 3 次 (NotImplementedError)
    ↓
Monitor 全局 counter=3, _failure_feedback_sent={run_id}
    ↓
反馈文本(seq=13):
 "Warning: 3 consecutive failures. Consider checking parameters or terminating."
    ↓
(feedback 只进了 think 路径，不进 revise，不进 plan)
_revise() 中的 prompt 没有 feedback 节，没有 intent（显示 unknown）
    ↓
LLM 自行切换为 http_request（运气好，与反馈无关）
但输出中 action/url 在 step 顶级，不在 input 内
    ↓
_parse_plan → input=None → return None
Retry 消息只说"不是有效 JSON"，LLM 不知道具体 schema 问题
    ↓
3 次 Parse failed → revise failed → RunFailed
    ↓
真实死因: schema 不兼容，不是反馈
```

### 改造后（含 schema 修复 + 反馈修复）

```
browser 连续 3 次 NotImplementedError
    ↓
Monitor per-tool 追踪: browser=3, NotImplementedError=3
纯度检查: 3/3=100% ≥80%
_has_active_feedback() 查 EventStore → 无同类反馈 → 注入
    ↓
结构化反馈(expires_at_seq=+50):
 "!! [HIGH] Tool 'browser' failed 3/3 with 'NotImplementedError'
  → Use http_request for web requests."
    ↓
Scheduler 传 feedback(for_revise=True) 给 Planner.revise()
_REVISE_PROMPT 出现反馈节 + intent 正确传递 + schema 示例提示
    ↓
LLM 看到建议 → 生成 http_request plan
且因 prompt 提示了 input 嵌套 + retry 带具体错误
    ↓
LLM 输出正确结构 {"input": {"action": "GET", "url": "..."}}
_parse_plan 新兼容逻辑也兜底
    ↓
修订计划执行成功
```

---

## 6. 验证方式

| # | 测试 | 类型 | 验证内容 |
|---|------|------|----------|
| 1 | `test_structured_feedback_payload` | 单元 | 新字段序列化/反序列化；新旧兼容；`priority` 支持 `"low"` |
| 2 | `test_per_tool_failure_tracking` | 单元 | 3 次 browser 失败 → feedback 含 `affected_tool=browser` |
| 3 | `test_guardrail_per_tool_tracking` | 单元 | GUARDRAIL_TRIGGERED 也被 per-tool 追踪 |
| 4 | `test_mixed_tool_failure_no_suggestion` | 单元 | 混合工具失败(2 browser + 1 http) → suggestion=None |
| 5 | `test_multi_mode_feedback` | 单元 | browser 失败 3 次 → 反馈注入后 http 又失败 3 次 → 第二次新反馈注入（修复 Bug E；验证通用多模式方案：防重 key=(tool,error)，非 dominant 推导） |
| 6 | `test_error_type_not_message` | 单元 | `PlaywrightError: X` 和 `PlaywrightError: Y` 归为同 error_type |
| 7 | `test_feedback_deterministic_id` | 单元 | 相同输入产生相同 feedback_id；不同输入不同 ID |
| 8 | `test_no_duplicate_from_eventstore` | 集成 | 同类反馈已存在时跳过注入 |
| 9 | `test_condition_resolved_same_tool_only` | 单元 | browser 失败→http_request 成功→不发 RESOLVED |
| 10 | `test_condition_resolved_only_dominant` | 单元 | browser 失败→browser 成功→发 RESOLVED |
| 11 | `test_feedback_expiration` | 单元 | expires_at_seq < state.seq → 不展示 |
| 12 | `test_feedback_for_revise_filter` | 单元 | for_revise=True → 仅 high + operator |
| 13 | `test_feedback_source_display` | 单元 | OPERATOR 反馈含 `[Operator]` 标签 |
| 14 | `test_planner_revise_feedback_injection` | 集成 | revise() 收到 feedback → prompt 含反馈节 |
| 15 | `test_planner_plan_feedback_injection` | 集成 | plan() 收到 feedback → prompt 含反馈节 |
| 16 | `test_jsonschema_step_validation_rejects_wrong_fields` | 单元 | step 中 `action`/`url` 在顶层 → `jsonschema.validate()` 报 `ValidationError`，message 带字段名 |
| 17 | `test_jsonschema_step_validation_passes_correct` | 单元 | step 中 `action`/`url` 嵌套在 `input` → 校验通过 |
| 18 | `test_parse_plan_uses_jsonschema_error_message` | 单元 | _parse_plan 校验失败 → error_reason 包含具体字段名和 schema 提示 |
| 19 | `test_parse_plan_valid_case` | 单元 | 标准格式正常解析 |
| 20 | `test_both_prompts_share_same_schema_text` | 单元 | `_PLAN_PROMPT` 和 `_REVISE_PROMPT` 的 schema 描述完全一致 |
| 21 | `test_schema_text_contains_input_nesting_instruction` | 单元 | schema 描述中明确告知 `input` 必需且参数不能放 step 顶层 |
| 22 | `test_revise_intent_not_unknown` | 集成 | revise() 收到 intent_fallback → prompt 不出现 `(unknown)`（修复 Bug D） |
| 23 | `test_operator_feedback_api` | 集成 | POST 反馈 → EventStore 查到 source=operator |
| 24 | `test_operator_feedback_id` | 单元 | Operator 反馈有确定性 feedback_id，非空 |
| 25 | `test_feedback_survives_checkpoint` | 集成 | ContextManager checkpoint 后 feedback 保留 |
| 26 | 全部现有 378 行测试 | 回归 | 零 breakage |

---

## 7. 实施顺序

| 步骤 | 内容 | 依赖 | 修复的 Bug |
|------|------|------|------------|
| 0 | **JSON Schema 统一治理** — 定义 `_STEP_SCHEMA_SIMPLE` → `_build_step_schema_text()` 驱动两 prompt schema 描述 → `_validate_step()` 用 `jsonschema.validate()` → `_parse_plan` 返回结构化错误 → retry 带具体字段错误 | 无 | **Bug A（schema 三处脱节）, G（retry 零诊断）** |
| 1 | 改 `FeedbackInjectedPayload` — 加新字段 + `FeedbackCategory` + `FeedbackSource` + `priority` 加 `"low"` | 无 | P0—反馈内容, P1—过期, Bug H |
| 2 | 改 `RunMonitor` — per-tool 追踪 + 统一检测 + EventStore 推导防重(category 粒度) + 建议生成 + 分辨率 + 确定性 hash + 多模式反馈支持 | 步骤 1 | P0—反馈内容, P1—过期, Bug E, F |
| 3 | 改 `Scheduler._get_feedback_text` — 结构化渲染 + for_revise 过滤 + 过期/已解决过滤 | 步骤 1 | P0—建议缺失 |
| 4 | 改 `Planner` — `revise()` 和 `plan()` 加 feedback 参数 + prompt 加 `{feedback_section}` + **intent 修复(传 intent_fallback)** | 步骤 3 | P0—反馈链路, Bug D |
| 5 | 改 `PlanningExecutorScheduler` — 所有 5 个 revise/plan 调用点传 feedback | 步骤 4 | P0—反馈链路 |
| 6 | 加 Operator API 端点 + feedback_id 计算 | 步骤 1 | Bug (Operator feedback_id 空) |
| 7 | 写测试（覆盖所有修复点，含 schema 兼容、多模式反馈、retry 错误信息）| 步骤 0-6 | 全部 |

---

## 8. 架构变更 vs 丢弃项

经过架构审查（2026-06-08），以下设计决策已确认：

| 建议 | 判定 | 理由 |
|------|------|------|
| JSON Schema 统一驱动（`_build_step_schema_text` + `jsonschema.validate`） | **必须修** | 根治 schema 三处脱节；复用项目中已有 `jsonschema` 和 `guardrails.py`/`executor.py` 模式 |
| `_REVISE_PROMPT` 和 `_PLAN_PROMPT` 共用同一 schema 描述 | **必须修** | 确保两处结构一致，消除 "same as before" 的歧义 |
| retry 消息带具体 `ValidationError.message` + `path` | **必须修** | 否则 LLM 反复输出相同错误格式，3 次全浪费 |
| `feedback_id` 去 `time.time()` | **必须修** | 确定性是 resolution 关联的前置条件 |
| `_failure_feedback_sent` 换 EventStore 推导 + per-category 粒度防重 | **必须修** | 否则 Monitor 重启重复注入 + 故障模式变化后无法触发新反馈 |
| `revise()` intent 修复（传 intent_fallback） | **必须修** | 否则 revise prompt 中 `(unknown)`，Agent 无上下文 |
| CONDITION_RESOLVED 仅同工具触发 | **必须修** | 否则语义错误 |
| dominant_tool 纯度阈值 80% | **必须修** | 否则混合失败给错误建议 |
| error_type 分离异常类名和 message | **必须修** | 否则不同错误被合并 |
| `priority` 加 `"low"` 级别 | **必须修** | CONDITION_RESOLVED 需要 low 级别 |
| plan() 也传 feedback | **应该修** | 否则动态规划模式反馈丢失 |
| 加 source 字段 (monitor/operator) | **应该修** | 优先级和显示区分 |
| revise 仅传 high 优先级 | **应该修** | 防止 Planner 被噪音淹没 |
| Operator feedback_id 计算 | **必须修** | 当前 feedback_id 为空，无法被 resolution 关联 |
| 生命周期文档补全 | **应该修** | Event Sourcing 自然保证，需注明 |
| expires_at_seq 双条件 (seq+time) | **低优** | seq-only 当前够用，留注释 |
| FailureAdvisor 注册机制 | **丢弃** | 当前 4 工具硬编码够用，加 TODO 注释 |
| FeedbackCategory 拆三维 | **丢弃** | 概念复杂度 > 实际收益 |
| 独立 FEEDBACK_RESOLVED 事件类型 | **丢弃 (V1.0)** | 兼容成本高，当前 category 方式工作正常 |

---

*文档基于 `AGENTS.md` 第 3.4 节三对齐审查要求生成*
*核心架构参考 `harness_v2.1.md` 受信边界约束*
*架构审查反馈处理后版本: v1.2*
