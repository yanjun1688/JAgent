# Harness 监控反馈机制改造方案

> **当前阶段**: 设计文档
> **关联里程碑**: V0.6 — 监控与反馈系统（增强）
> **文档版本**: v1.0
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
│  只有 feedback_text + priority，没有 tool/error/suggestion/expires 等结构
│
├─ RunMonitor（run_monitor.py:69-81）
│  只计数"连续失败次数"，不记录"哪个工具"、"什么错误"
│  TOOL_FAILED 和 GUARDRAIL_TRIGGERED 两分支各自独立检测，无统一模式识别
│  生成的是固定字符串模板，无上下文感知
│
├─ Scheduler._get_feedback_text（scheduler.py:130-136）
│  只是 "\n".join(feedback_text)，优先级被丢弃
│
├─ Planner.revise（planner.py:67-82）
│  _REVISE_PROMPT 模板完全没有 {feedback} 占位位
│  revise() 方法签名也没有 feedback 参数
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
4. **一条反馈解决一个问题** — 不改变事件溯源的核心模式
5. **确定性去重** — feedback_id 使用 deterministic hash，防止 `on_append` 回调重入

### 2.2 改动一览

| 文件 | 改动类型 | 行数 |
|------|----------|------|
| `harness/models/events.py` | 增强 `FeedbackInjectedPayload`，加 6 个可选字段 + `FeedbackCategory` 枚举 | ~20 |
| `harness/monitoring/run_monitor.py` | 重构检测逻辑：per-tool 追踪 + 错误模式识别 + GUARDRAIL_TRIGGERED 统一追踪 + 建议生成 + 分辨率信号 + 确定性 hash 去重 + expires_at_seq 三级策略 | ~100 |
| `harness/core/scheduler.py` | 改进反馈渲染格式 + `_get_feedback_text()` 过滤过期 + 被解决的反馈 | ~50 |
| `harness/core/planner.py` | `_REVISE_PROMPT` 加 `{feedback_section}` + `revise()` 加 `feedback` 参数 | ~25 |
| `harness/api/routes.py` | 新增 `POST /api/v1/runs/{run_id}/feedback` | ~30 |
| `tests/test_monitoring.py` | 新增 ~5 个测试用例 | ~120 |

**总计：约 350 行代码（含测试）**

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
    CONDITION_RESOLVED = "condition_resolved"  # 新：标记某条旧反馈已解决

class FeedbackInjectedPayload(BaseModel):
    feedback_id: str = ""                     # 确定性 hash，用于去重和关联 resolution
    category: FeedbackCategory = FeedbackCategory.OPERATOR_ADVICE  # 默认保兼容
    feedback_text: str                        # 不变，保留向后兼容
    priority: Literal["high", "medium", "low"] = "medium"
    affected_tool: str | None = None          # 哪个工具出问题
    error_type: str | None = None             # 错误类型（NotImplementedError）
    error_detail: str | None = None           # 详细错误信息（前 200 字）
    suggestion: str | None = None             # 具体替代建议
    expires_at_seq: int | None = None         # 在此 seq 后自动过期
    resolves_feedback_id: str | None = None   # 解决了哪条旧反馈
```

**关键设计**：所有新字段都是 `Optional`。老代码写的不带新字段的反馈可以正常 fold 和展示，零兼容成本。

### 3.2 feedback_id 确定性 hash

```python
import hashlib, time

feedback_id = hashlib.sha256(
    f"{run_id}:{category.value}:{feedback_text[:100]}:{int(time.time() // 60)}"
    .encode()
).hexdigest()[:16]
```

时间窗口为 1 分钟。同一分钟内同一条反馈不会重复写入。防止 `on_append` 回调重入或 EventStore 重试导致重复事件。

### 3.3 expires_at_seq 三级策略

| Priority | 含义 | expires_at_seq = current_seq + |
|----------|------|-------------------------------|
| high | 严重问题需修复 | **+50** (~10-12 次迭代，有充分时间修复) |
| medium | 建议性预警 | **+30** (~6-8 次迭代) |
| low | 信息 / 已解决 | **+10** (~2-3 次迭代，快速消失) |

### 3.4 RunMonitor 增强

**文件**: `harness/monitoring/run_monitor.py`

#### 3.4.1 新增追踪状态

```python
self._failures_per_tool: dict[str, dict[str, int]] = {}         # run_id → {tool_name: fail_count}
self._failure_error_map: dict[str, dict[str, dict[str, int]]] = {}  # run_id → {tool_name: {error: count}}
```

#### 3.4.2 统一模式识别

`_on_event_impl()` 中 TOOL_FAILED 和 GUARDRAIL_TRIGGERED 两个分支统一走同一套逻辑：

```
TOOL_FAILED ─┐
              ├─ per_tool[rid][tool] += 1
              ├─ per_error[rid][tool][error_key] += 1
GUARDRAIL_    │
TRIGGERED ────┘
              │
              └─ 公共方法 _check_and_inject_feedback(rid)
                  ├─ 判断 count >= 3
                  ├─ 识别 dominant_tool + dominant_error
                  ├─ 调用 _generate_suggestion(dominant_tool, dominant_error)
                  └─ 写入结构化 FeedbackInjected
```

**新逻辑伪代码**：

```python
async def _on_event_impl(self, event: Event) -> None:
    rid = event.run_id

    if event.event_type in (EventType.TOOL_FAILED, EventType.GUARDRAIL_TRIGGERED):
        tool = event.payload.get("tool_name", "?")
        error = event.payload.get("error", "")
        
        count = self._consecutive_failures.get(rid, 0) + 1
        self._consecutive_failures[rid] = count
        
        per_tool = self._failures_per_tool.setdefault(rid, {})
        per_tool[tool] = per_tool.get(tool, 0) + 1
        
        err_map = self._failure_error_map.setdefault(rid, {}).setdefault(tool, {})
        err_key = error.split(":")[0]
        err_map[err_key] = err_map.get(err_key, 0) + 1
        
        if count >= 3 and rid not in self._failure_feedback_sent:
            self._failure_feedback_sent.add(rid)
            await self._check_and_inject_feedback(rid)
```

#### 3.4.3 公共反馈触发方法

```python
async def _check_and_inject_feedback(self, rid: str) -> None:
    per_tool = self._failures_per_tool.get(rid, {})
    err_map = self._failure_error_map.get(rid, {})
    
    dominant_tool = max(per_tool, key=per_tool.get) if per_tool else "?"
    tool_errors = err_map.get(dominant_tool, {})
    dominant_error = max(tool_errors, key=tool_errors.get) if tool_errors else "unknown"
    suggestion = self._generate_suggestion(dominant_tool, dominant_error)
    
    category = FeedbackCategory.GUARDRAIL_TRIGGERED if ... else FeedbackCategory.TOOL_FAILURE
    
    await self._inject_feedback(
        rid, "high",
        feedback_text=f"Tool '{dominant_tool}' failed {per_tool.get(dominant_tool, 0)} times with '{dominant_error}'",
        category=category,
        affected_tool=dominant_tool,
        error_type=dominant_error,
        error_detail=first_error_detail[:200] if first_error_detail else None,
        suggestion=suggestion,
        expires_at_seq=current_seq + 50,
    )
```

#### 3.4.4 建议生成器

```python
def _generate_suggestion(self, tool: str, error_type: str) -> str | None:
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

```python
if event.event_type == EventType.TOOL_COMPLETED:
    was = self._consecutive_failures.get(rid, 0)
    self._consecutive_failures[rid] = 0
    self._failure_feedback_sent.discard(rid)
    
    if was >= 3:
        await self._inject_feedback(
            rid, "low",
            feedback_text="Failure streak resolved",
            category=FeedbackCategory.CONDITION_RESOLVED,
            resolves_feedback_id=previous_fb_id,
            expires_at_seq=current_seq + 10,
        )
```

#### 3.4.6 增强的 `_inject_feedback`

```python
async def _inject_feedback(self, run_id, priority, feedback_text, *,
                           category=FeedbackCategory.OPERATOR_ADVICE,
                           affected_tool=None, error_type=None, error_detail=None,
                           suggestion=None, expires_at_seq=None,
                           resolves_feedback_id=None) -> str:
    feedback_id = hashlib.sha256(
        f"{run_id}:{category.value}:{feedback_text[:100]}:{int(time.time()//60)}"
        .encode()
    ).hexdigest()[:16]
    
    payload = FeedbackInjectedPayload(
        feedback_id=feedback_id, category=category,
        feedback_text=feedback_text, priority=priority,
        affected_tool=affected_tool, error_type=error_type,
        error_detail=error_detail, suggestion=suggestion,
        expires_at_seq=expires_at_seq,
        resolves_feedback_id=resolves_feedback_id,
    )
    await self.store.append_event(run_id, EventType.FEEDBACK_INJECTED, payload.model_dump())
    return feedback_id
```

### 3.5 反馈渲染 + 过期过滤

**文件**: `harness/core/scheduler.py`

#### 3.5.1 格式化方法

```python
def _format_feedback(self, fb: FeedbackInjectedPayload) -> str:
    if fb.category == FeedbackCategory.CONDITION_RESOLVED:
        return f"[RESOLVED] {fb.feedback_text}"
    
    level = "!!" if fb.priority == "high" else "!" if fb.priority == "medium" else ""
    parts = [f"{level} [{fb.priority.upper()}] {fb.feedback_text}"]
    if fb.affected_tool and fb.error_type:
        parts.append(f"   Tool: {fb.affected_tool}")
        parts.append(f"   Error: {fb.error_type}")
    if fb.error_detail:
        parts.append(f"   Detail: {fb.error_detail[:120]}")
    if fb.suggestion:
        parts.append(f"   → {fb.suggestion}")
    return "\n".join(parts)
```

#### 3.5.2 过滤 + 渲染

```python
def _get_feedback_text(self, state: RunState) -> str | None:
    if not self.monitor:
        return None
    
    active = [
        fb for fb in state.feedbacks[-10:]
        if (fb.expires_at_seq is None or state.seq <= fb.expires_at_seq)
        and fb.category != FeedbackCategory.CONDITION_RESOLVED
    ]
    
    resolved_ids = {
        fb.resolves_feedback_id for fb in state.feedbacks[-10:]
        if fb.category == FeedbackCategory.CONDITION_RESOLVED and fb.resolves_feedback_id
    }
    active = [fb for fb in active if fb.feedback_id not in resolved_ids]
    
    if not active:
        return None
    
    rendered = [self._format_feedback(fb) for fb in active[-5:]]
    return (
        "## Monitoring Feedback\n"
        + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n".join(rendered)
    )
```

**Agent 看到的效果**：

```
## Monitoring Feedback
!! [HIGH] Tool 'browser' failed 3 times with 'NotImplementedError'
   Tool: browser
   Error: NotImplementedError
   Detail: Browser action 'navigate' failed: NotImplementedError:
   → The browser tool is unavailable. Use 'http_request' for web requests.
```

### 3.6 Planner Revise 注入

**文件**: `harness/core/planner.py`

#### 3.6.1 `_REVISE_PROMPT` 加 `{feedback_section}`

```python
_REVISE_PROMPT = """You are a task planner reviewing execution results.
Some steps completed, some may have failed. Decide what to do next.

## Original User Intent
{intent}

{system_state}

{feedback_section}

## Output JSON format — same as before:
...

## Available Tools
{tool_descriptions}
"""
```

#### 3.6.2 `revise()` 方法新增 `feedback` 参数

```python
async def revise(
    self,
    plan: DagPlan,
    results: dict[str, Any],
    system_state: str,
    feedback: str | None = None,  # 新参数
) -> DagPlan | None:
    feedback_section = ""
    if feedback:
        feedback_section = (
            f"## System Monitoring Feedback\n"
            f"{feedback}\n"
            f"Take this feedback into account when revising the plan.\n"
        )
    
    prompt = _REVISE_PROMPT.format(
        intent=plan.intent[:200] if plan.intent else "(unknown)",
        system_state=system_state,
        feedback_section=feedback_section,
        tool_descriptions=self._build_tool_descriptions(),
    )
    ...
```

#### 3.6.3 Scheduler 传入反馈

```python
async def _execute_static_plan(self, run_id, plan, consecutive_failures):
    for layer_idx, layer in enumerate(layers):
        ...
        if not ok:
            current_state = await self._refresh_state(run_id)
            feedback_text = self._get_feedback_text(current_state)
            
            revised = await self.planner.revise(
                plan, results, sys_state, feedback=feedback_text
            )
```

同样模式应用于 `_execute_dynamic_plan()` 中的 revise 调用。

### 3.7 Operator 手动反馈 API

**文件**: `harness/api/routes.py`

```python
@router.post("/api/v1/runs/{run_id}/feedback")
async def operator_feedback(
    run_id: str,
    body: OperatorFeedbackRequest,
    hapi: HarnessAPI = Depends(get_hapi),
):
    """Operator 在运行中注入手动反馈，引导 Agent 行为。"""
    payload = FeedbackInjectedPayload(
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

## 4. 数据流对比

### 改造前

```
browser 连续失败 3 次
    ↓
Monitor 发现 "3 consecutive failures"
    ↓
反馈: "Warning: 3 consecutive failures. Consider checking parameters or terminating."
    ↓
(feedback 只进了 think 路径，不进 revise)
    ↓
Planner revise 尝试相同模式 3 次，全部失败
    ↓
RunFailed
```

### 改造后

```
browser 连续 3 次 NotImplementedError
    ↓
Monitor 识别模式: per_tool[browser]=3, per_error[NotImplementedError]=3
    ↓
反馈: "!! [HIGH] Tool 'browser' failed 3 times with 'NotImplementedError'
   → Use http_request for web requests instead."
   (expires_at_seq = current_seq + 50)
    ↓
Scheduler 取反馈传给 Planner.revise(..., feedback=...)
    ↓
_REVISE_PROMPT 中出现反馈节:
   "## System Monitoring Feedback
    browser 不可用，建议改用 http_request"
    ↓
LLM 看到建议，生成了 http_request 的 revise plan
    ↓
(如果后续某步 TOOL_COMPLETED 让失败计数归零)
    ↓
Monitor 发 CONDITION_RESOLVED，旧反馈自动被隐藏
```

---

## 5. 测试策略

| # | 测试 | 类型 | 验证内容 |
|---|------|------|----------|
| 1 | `test_structured_feedback_payload` | 单元 | 新字段序列化/反序列化 |
| 2 | `test_per_tool_failure_tracking` | 单元 | 3 次 browser 失败 → feedback 含 `affected_tool=browser` |
| 3 | `test_guardrail_per_tool_tracking` | 单元 | GUARDRAIL_TRIGGERED 也被 per-tool 追踪 |
| 4 | `test_error_pattern_suggestion` | 单元 | NotImplementedError → suggestion 含 http_request |
| 5 | `test_condition_resolved_injection` | 单元 | 失败 3 次后成功 1 次 → 发 CONDITION_RESOLVED |
| 6 | `test_feedback_expiration` | 单元 | expires_at_seq < state.seq → 不展示 |
| 7 | `test_feedback_deterministic_id` | 单元 | 相同输入产生相同 feedback_id |
| 8 | `test_planner_revise_feedback_injection` | 集成 | revise() 收到 feedback → prompt 含反馈节 |
| 9 | `test_operator_feedback_api` | 集成 | POST 反馈 → EventStore 可查到事件 |
| 10 | 全部现有 378 行测试 | 回归 | 零 breakage |

---

## 6. 实施顺序

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | 改 `FeedbackInjectedPayload` — 加新字段 + `FeedbackCategory` 枚举 | 无 |
| 2 | 改 `RunMonitor` — per-tool 追踪 + 统一 TOOL_FAILED/GUARDRAIL_TRIGGERED 检查 + 建议生成 + 分辨率 + 确定性 hash | 步骤 1 |
| 3 | 改 `Scheduler._get_feedback_text` — 结构化渲染 + 过期/已解决过滤 | 步骤 1 |
| 4 | 改 `Planner` — `revise()` 加 `feedback` 参数 + `_REVISE_PROMPT` 加 `{feedback_section}` | 步骤 3 |
| 5 | 改 `PlanningExecutorScheduler` — revise 调用传 feedback | 步骤 4 |
| 6 | 加 Operator API 端点 | 步骤 1 |
| 7 | 写测试 | 步骤 1-6 |

---

*文档基于 `AGENTS.md` 第 3.4 节三对齐审查要求生成*
*核心架构参考 `harness_v2.1.md` 受信边界约束*
