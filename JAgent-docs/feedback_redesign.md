# Harness 监控反馈机制改造方案

> **当前阶段**: 设计文档
> **关联里程碑**: V0.6.1 — 反馈机制增强
> **文档版本**: v1.1
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

### 1.2 四个独立问题

| # | 问题 | 严重性 | 影响 |
|---|------|--------|------|
| P0 | 反馈内容宽泛，无具体信息 | 致命 | 没说哪个工具 (`browser`)、什么错误 (`NotImplementedError`)、模式（同工具同错） |
| P0 | 没有建设性建议 | 致命 | 只说"检查参数或终止"，没说"browser 不可用，改用 http_request" |
| P0 | 反馈流不到 Planner revise | 致命 | `Planner.revise()` 用 `_REVISE_PROMPT`，这个模板没有 `{feedback}` 占位位，反馈发了 Agent 看不见 |
| P1 | 反馈永不过期 | 严重 | 一次"3 次连续失败"反馈永久存在，后面成功了还留着，混淆 Agent |

### 1.3 代码根因

```
┌─ FeedbackInjectedPayload（events.py:142-144）
│  只有 feedback_text + priority，没有 tool/error/suggestion/expires/source 等结构
│
├─ RunMonitor（run_monitor.py:69-81）
│  只计数"连续失败次数"，不记录"哪个工具"、"什么错误"
│  TOOL_FAILED 和 GUARDRAIL_TRIGGERED 两分支各自独立检测，无统一模式识别
│  生成的是固定字符串模板，无上下文感知
│
├─ Scheduler._get_feedback_text（scheduler.py:130-136）
│  只是 "\n".join(feedback_text)，优先级被丢弃
│
├─ Planner.revise + Planner.plan（planner.py:67-82）
│  _REVISE_PROMPT 和 _PLAN_PROMPT 都没有 {feedback} 占位位
│  revise()/plan() 方法签名也没有 feedback 参数
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
| `harness/models/events.py` | 增强 `FeedbackInjectedPayload`，加 8 个可选字段 + `FeedbackCategory` + `FeedbackSource` 枚举 | ~30 |
| `harness/monitoring/run_monitor.py` | 重构检测逻辑：per-tool 追踪 + 错误模式识别 + GUARDRAIL_TRIGGERED 统一追踪 + 建议生成 + 分辨率信号 + EventStore 推导防重 + 确定性 hash + expires_at_seq 三级策略 | ~130 |
| `harness/core/scheduler.py` | 改进反馈渲染格式 + `_get_feedback_text()` 新增 `for_revise` 参数（仅 high） + 过滤过期/被解决的反馈 + source 不同渲染 | ~60 |
| `harness/core/planner.py` | `_REVISE_PROMPT` 加 `{feedback_section}` + `revise()` 加 `feedback` 参数 + `_PLAN_PROMPT` 加 `{feedback_section}` + `plan()` 加 `feedback` 参数 | ~40 |
| `harness/api/routes.py` | 新增 `POST /api/v1/runs/{run_id}/feedback` | ~30 |
| `tests/test_monitoring.py` | 新增 ~8 个测试用例 | ~150 |

**总计：约 440 行代码（含测试）**

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
self._dominant_tool_cache: dict[str, str | None] = {}               # run_id → 当前 dominant tool（用于分辨率判断）
```

> 注意：这些是 Monitor 进程内存态，Monitor 重启后重建。依赖这些状态的失效保护见 3.4.3。

#### 3.4.2 统一模式识别

TOOL_FAILED 和 GUARDRAIL_TRIGGERED 统一走同一套逻辑：

```
TOOL_FAILED ─┐
              ├─ per_tool[rid][tool] += 1
              ├─ per_error[rid][tool][error_type] += 1
GUARDRAIL_    │
TRIGGERED ────┘
              │
              └─ count >= 3 → _check_and_inject_feedback(rid)
                  ├─ 识别 dominant_tool + dominant_error
                  ├─ 纯度检查（dominant 占比 ≥80% 才给工具级建议）
                  ├─ 查询 EventStore 有无同类别 active 反馈（防重）
                  └─ 写入结构化 FeedbackInjected
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
            await self._check_and_inject_feedback(rid)

def _extract_error_type(self, error_text: str) -> str:
    """提取异常类名，不解析 message。
    
    'NotImplementedError: ...' → 'NotImplementedError'
    'PlaywrightError: Browser closed unexpectedly' → 'PlaywrightError'
    'TimeoutError' → 'TimeoutError'
    """
    return error_text.split(":")[0].strip() if ":" in error_text else error_text.strip()
```

#### 3.4.3 公共反馈触发方法（含 EventStore 防重）

```python
async def _check_and_inject_feedback(self, rid: str) -> None:
    per_tool = self._failures_per_tool.get(rid, {})
    err_map = self._failure_error_map.get(rid, {})
    
    if not per_tool:
        return
    
    total = sum(per_tool.values())
    dominant_tool = max(per_tool, key=per_tool.get)
    dominant_count = per_tool[dominant_tool]
    
    tool_errors = err_map.get(dominant_tool, {})
    dominant_error = max(tool_errors, key=tool_errors.get) if tool_errors else "unknown"
    
    # 纯度检查：混合工具失败时不提供工具级建议
    is_mixed = (dominant_count / total) < 0.8
    suggestion = None if is_mixed else self._generate_suggestion(dominant_tool, dominant_error)
    
    category = FeedbackCategory.GUARDRAIL_TRIGGERED if ... else FeedbackCategory.TOOL_FAILURE
    
    # 查询 EventStore：同类别 + 同 tool + 同 error 的反馈是否已存在
    current_seq = ...  # 从事件获取
    if await self._has_active_feedback(rid, category, dominant_tool, dominant_error):
        return  # 已有活跃反馈，不重复注入
    
    self._dominant_tool_cache[rid] = dominant_tool
    
    await self._inject_feedback(
        rid, "high",
        feedback_text=f"Tool '{dominant_tool}' failed {dominant_count}/{total} times with '{dominant_error}'",
        category=category,
        affected_tool=dominant_tool,
        error_type=dominant_error,
        error_detail=event.payload.get("error", "")[:200],
        suggestion=suggestion,
        expires_at_seq=current_seq + 50,
    )

async def _has_active_feedback(
    self, rid: str, category: FeedbackCategory,
    tool: str | None, error: str | None,
) -> bool:
    """从 EventStore 折叠状态推导是否已有同类活跃反馈。
    
    替代旧的 _failure_feedback_sent 内存集合，保证 Monitor 重启后不重复注入。
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

条件收紧：**仅当连续失败数量 ≥3 且本次成功的是 dominant tool 时**，才发 CONDITION_RESOLVED。

```python
if event.event_type == EventType.TOOL_COMPLETED:
    was = self._consecutive_failures.get(rid, 0)
    tool = event.payload.get("tool_name", "")
    self._consecutive_failures[rid] = 0
    
    if was >= 3 and tool == self._dominant_tool_cache.get(rid):
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
    self._dominant_tool_cache.pop(run_id, None)
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
        intent=...,
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

#### 3.6.3 Scheduler 传入反馈

`PlanningExecutorScheduler` 中 revise 和 plan 的调用点都传入 feedback。

```python
# revise 路径
current_state = await self._refresh_state(run_id)
feedback_text = self._get_feedback_text(current_state, for_revise=True)
revised = await self.planner.revise(plan, results, sys_state, feedback=feedback_text)

# plan 路径（动态规划模式重新规划时）
feedback_text = self._get_feedback_text(state, for_revise=True)
plan = await self._get_or_fallback(run_id, intent, state, feedback_text)
# → _get_or_fallback 传入 planner.plan(intent, state, feedback=feedback_text)
```

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
browser 连续失败 3 次
    ↓
Monitor 发现 "3 consecutive failures"
    ↓
反馈: "Warning: 3 consecutive failures. Consider checking parameters or terminating."
    ↓
(feedback 只进了 think 路径，不进 revise，不进 plan)
    ↓
Planner revise 尝试相同模式 3 次，全部失败
    ↓
RunFailed
```

### 改造后

```
browser 连续 3 次 NotImplementedError
    ↓
Monitor per-tool 追踪: browser=3, NotImplementedError=3
    ↓
纯度检查: 3/3=100% ≥80% → 生成工具级建议
_has_active_feedback() 查 EventStore → 无同类反馈 → 注入
    ↓
反馈(expires_at_seq=+50):
 "!! [HIGH] Tool 'browser' failed 3/3 with 'NotImplementedError'
  → Use http_request for web requests."
    ↓
Scheduler 传 feedback(for_revise=True) 给 Planner.revise()
_REVISE_PROMPT 出现反馈节 → LLM 看到建议
    ↓
LLM 生成 http_request 的 revised plan → 执行
    ↓
(如果 browser 最终成功了)
Monitor: TOOL_COMPLETED tool=browser → tool==dominant → 发 CONDITION_RESOLVED
旧反馈自动隐藏
```

---

## 6. 验证方式

| # | 测试 | 类型 | 验证内容 |
|---|------|------|----------|
| 1 | `test_structured_feedback_payload` | 单元 | 新字段序列化/反序列化；新旧兼容 |
| 2 | `test_per_tool_failure_tracking` | 单元 | 3 次 browser 失败 → feedback 含 `affected_tool=browser` |
| 3 | `test_guardrail_per_tool_tracking` | 单元 | GUARDRAIL_TRIGGERED 也被 per-tool 追踪 |
| 4 | `test_mixed_tool_failure_no_suggestion` | 单元 | 混合工具失败(2 browser + 1 http) → suggestion=None |
| 5 | `test_error_type_not_message` | 单元 | `PlaywrightError: X` 和 `PlaywrightError: Y` 归为同 error_type |
| 6 | `test_feedback_deterministic_id` | 单元 | 相同输入产生相同 feedback_id；不同输入不同 ID |
| 7 | `test_no_duplicate_from_eventstore` | 集成 | 同类反馈已存在时跳过注入 |
| 8 | `test_condition_resolved_same_tool_only` | 单元 | browser 失败→http_request 成功→不发 RESOLVED |
| 9 | `test_condition_resolved_only_dominant` | 单元 | browser 失败→browser 成功→发 RESOLVED |
| 10 | `test_feedback_expiration` | 单元 | expires_at_seq < state.seq → 不展示 |
| 11 | `test_feedback_for_revise_filter` | 单元 | for_revise=True → 仅 high + operator |
| 12 | `test_feedback_source_display` | 单元 | OPERATOR 反馈含 `[Operator]` 标签 |
| 13 | `test_planner_revise_feedback_injection` | 集成 | revise() 收到 feedback → prompt 含反馈节 |
| 14 | `test_planner_plan_feedback_injection` | 集成 | plan() 收到 feedback → prompt 含反馈节 |
| 15 | `test_operator_feedback_api` | 集成 | POST 反馈 → EventStore 查到 source=operator |
| 16 | `test_feedback_survives_checkpoint` | 集成 | ContextManager checkpoint 后 feedback 保留 |
| 17 | 全部现有 378 行测试 | 回归 | 零 breakage |

---

## 7. 实施顺序

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | 改 `FeedbackInjectedPayload` — 加新字段 + `FeedbackCategory` + `FeedbackSource` | 无 |
| 2 | 改 `RunMonitor` — per-tool 追踪 + 统一检测 + EventStore 防重 + 建议生成 + 分辨率 + 确定性 hash | 步骤 1 |
| 3 | 改 `Scheduler._get_feedback_text` — 结构化渲染 + for_revise 过滤 + 过期/已解决过滤 | 步骤 1 |
| 4 | 改 `Planner` — `revise()` 和 `plan()` 加 feedback 参数 + prompt 加 `{feedback_section}` | 步骤 3 |
| 5 | 改 `Scheduler` — 所有 revise/plan 调用点传 feedback | 步骤 4 |
| 6 | 加 Operator API 端点 | 步骤 1 |
| 7 | 写测试 | 步骤 1-6 |

---

## 8. 架构变更 vs 丢弃项

经过架构审查（2026-06-08），以下设计决策已确认：

| 建议 | 判定 | 理由 |
|------|------|------|
| `feedback_id` 去 `time.time()` | **必须修** | 确定性是 resolution 关联的前置条件 |
| `_failure_feedback_sent` 换 EventStore 推导 | **必须修** | 否则 Monitor 重启重复注入，违反事件溯源 |
| CONDITION_RESOLVED 仅同工具触发 | **必须修** | 否则语义错误 |
| dominant_tool 纯度阈值 80% | **必须修** | 否则混合失败给错误建议 |
| error_type 分离异常类名和 message | **必须修** | 否则不同错误被合并 |
| plan() 也传 feedback | **应该修** | 否则动态规划模式反馈丢失 |
| 加 source 字段 (monitor/operator) | **应该修** | 优先级和显示区分 |
| revise 仅传 high 优先级 | **应该修** | 防止 Planner 被噪音淹没 |
| 生命周期文档补全 | **应该修** | Event Sourcing 自然保证，需注明 |
| expires_at_seq 双条件 (seq+time) | **低优** | seq-only 当前够用，留注释 |
| FailureAdvisor 注册机制 | **丢弃** | 当前 4 工具硬编码够用，加 TODO 注释 |
| FeedbackCategory 拆三维 | **丢弃** | 概念复杂度 > 实际收益 |
| 独立 FEEDBACK_RESOLVED 事件类型 | **丢弃 (V1.0)** | 兼容成本高，当前 category 方式工作正常 |

---

*文档基于 `AGENTS.md` 第 3.4 节三对齐审查要求生成*
*核心架构参考 `harness_v2.1.md` 受信边界约束*
*架构审查反馈处理后版本: v1.1*
