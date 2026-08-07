# Langfuse 集成 — AI 编码任务 Prompt

> 将此文件内容完整复制到 AI 编码工具（Cursor、Claude Code、Copilot 等）中作为任务描述。

---

## 任务概述

为 JAgent（一个 Python Agent 执行引擎）集成 **Langfuse** 可观测性平台，实现 Agent 执行全链路 tracing 和离线评测。

**Langfuse Cloud 已配置就绪**，API Keys 在 `.env` 中：

```bash
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com
LANGFUSE_ENABLED=true
```

---

## 项目架构（关键信息）

### 受信/非受信边界（核心约束）

JAgent 有严格的**受信组件**和**非受信组件**划分。这是最重要的架构约束：

| 受信组件（系统强制） | 非受信组件（Agent 决策） |
|---------------------|------------------------|
| Event Store (SQLite append-only) | Agent Kernel (LLM 推理) |
| Tool Executor (8步执行流水线) | Planner (LLM 生成 DAG plan) |
| Guardrails (5种安全校验) | 工具实现 (browser, http, file_op 等) |
| Scheduler (循环控制) | |

**Langfuse 集成约束**：
1. Langfuse Tracer 属于**非受信组件**——纯观测层，对系统只读，不干预 Agent 决策
2. **不修改受信组件的执行逻辑**——埋点仅在现有步骤之间插入，不改变副作用执行顺序
3. **启用/禁用由环境变量 `LANGFUSE_ENABLED` 控制**，设为 `false` 时所有 tracer 方法为空操作，零性能开销
4. 异步 flush 通过 `asyncio.to_thread()` 放到线程池，不阻塞 Agent 循环

### 执行模式

1. **AgentLoopScheduler**（串行）：`harness/core/scheduler/loop.py` — think→act→observe 循环
2. **PlanningExecutorScheduler**（DAG 并行）：`harness/core/scheduler/plan.py` — Plan→Execute(parallel)→Revise（生产环境默认使用）

### 关键文件

| 文件 | 作用 |
|------|------|
| `harness/api/serve.py` | 入口，装配所有组件，读取 `.env` |
| `harness/api/deps.py` | HarnessAPI DI 容器，持有 store/executor/scheduler |
| `harness/core/llm_client.py` | LLM 客户端（OpenAI 兼容），`OpenAILLMClient.chat()` 方法是核心 |
| `harness/tools/executor.py` | 工具执行器，8 步流水线（guardrail→idempotency→confirm→execute→retry→semantic） |
| `harness/core/scheduler/base.py` | BaseScheduler 抽象基类，`__init__()` 签名见下 |
| `harness/core/scheduler/loop.py` | AgentLoopScheduler，`_run_loop()` 方法 |
| `harness/core/scheduler/plan.py` | PlanningExecutorScheduler |
| `harness/core/agent_kernel.py` | LLMAgentKernel，`think()` 方法 |
| `harness/monitoring/__init__.py` | 现有 monitoring 导出 `RunMonitor` |
| `pyproject.toml` | 依赖管理（uv + hatchling） |

---

## 实施任务（按 Phase 顺序执行）

### Phase 1: 基础设施 — 创建 LangfuseTracer 模块

#### 1.1 依赖

在 `pyproject.toml` 的 `dependencies` 中添加：

```toml
"langfuse>=2.0.0",
```

执行 `uv sync` 安装依赖。

#### 1.2 创建 `harness/monitoring/langfuse_tracer.py`（新文件）

核心类设计：

```python
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from langfuse import Langfuse


@dataclass
class TraceContext:
    """空对象模式——当 tracer 未启用时使用"""
    trace_id: str = ""
    enabled: bool = False

    def span(self, **kwargs) -> _NullSpan:
        return _NullSpan()

    def generation(self, **kwargs) -> _NullSpan:
        return _NullSpan()

    def event(self, **kwargs) -> None:
        pass


class _NullSpan:
    """空 span——所有方法空操作"""
    def end(self, **kwargs) -> None: pass
    def update(self, **kwargs) -> None: pass
    def generation(self, **kwargs) -> _NullSpan: return _NullSpan()
    def span(self, **kwargs) -> _NullSpan: return _NullSpan()
    def event(self, **kwargs) -> None: pass
    def score(self, **kwargs) -> None: pass
    def __enter__(self) -> _NullSpan: return self
    def __exit__(self, *args) -> None: pass


class LangfuseTracer:
    """Langfuse tracing 封装——非受信可观测性组件

    通过环境变量 LANGFUSE_ENABLED 控制启用/禁用。
    禁用时所有方法空操作，零开销降级。
    """

    def __init__(self):
        self._enabled = os.getenv("LANGFUSE_ENABLED", "").lower() == "true"
        self._pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self._sk = os.getenv("LANGFUSE_SECRET_KEY", "")
        self._host = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

        if self._enabled and self._pk and self._sk:
            self._client = Langfuse(
                public_key=self._pk,
                secret_key=self._sk,
                host=self._host,
            )
        else:
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def start_run(self, run_id: str, intent: str, scheduler_mode: str) -> TraceContext:
        """创建 Run 级别的 trace"""
        ...

    def end_run(self, ctx: TraceContext, status: str, output: str = "", error: str | None = None) -> None:
        """结束 trace，写入最终状态"""
        ...

    def start_iteration(self, ctx: TraceContext, iteration: int) -> TraceContext | None:
        """创建迭代 span"""
        ...

    def end_iteration(self, iter_ctx: TraceContext | None) -> None:
        """结束迭代 span"""
        ...

    def trace_llm_generation(
        self,
        ctx: TraceContext,
        model: str,
        messages: list[dict],
        response_content: str,
        tool_calls: list[str],
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: float,
    ) -> None:
        """记录 LLM 调用 (generation span)"""
        ...

    def trace_tool_execution(
        self,
        ctx: TraceContext,
        tool_name: str,
        tool_input: dict,
        tool_output: Any,
        status: str,
        duration_ms: int,
        error: str | None = None,
        cached: bool = False,
        retry_attempts: int = 0,
    ) -> None:
        """记录工具执行 (span)"""
        ...

    def trace_event(
        self,
        ctx: TraceContext,
        name: str,
        level: str = "DEFAULT",
        metadata: dict | None = None,
    ) -> None:
        """记录事件（guardrail/confirmation 等）"""
        ...

    def score(self, ctx: TraceContext, name: str, value: float, comment: str = "") -> None:
        """为 trace 评分"""
        ...

    async def flush_async(self) -> None:
        """异步 flush，通过线程池避免阻塞事件循环"""
        if self._client is not None:
            await asyncio.to_thread(self._client.flush)
```

**设计要求**：

1. `start_run()` 使用 `self._client.start_observation()` 或直接创建 Langfuse trace，返回 `TraceContext` 包装
2. 迭代 span 嵌套在 trace 下
3. LLM generation 嵌套在迭代 span 下
4. Tool span 嵌套在迭代 span 下
5. 所有方法在 `self._client is None` 时直接返回空对象，不抛异常
6. `TraceContext` 和 `_NullSpan` 实现空对象模式，使调用方无需 `if tracer.enabled:` 判断

#### 1.3 修改 `harness/monitoring/__init__.py`

```python
from harness.monitoring.run_monitor import RunMonitor
from harness.monitoring.langfuse_tracer import LangfuseTracer, TraceContext

__all__ = ["RunMonitor", "LangfuseTracer", "TraceContext"]
```

#### 1.4 修改 `harness/api/serve.py`

在文件末尾（`configure_hapi(api)` 之前），初始化 LangfuseTracer 并注入到 api：

```python
# ── 3.5 初始化 LangfuseTracer ────────────────────────────
from harness.monitoring import LangfuseTracer
tracer = LangfuseTracer()
api.tracer = tracer
_logger.info("Langfuse tracing: %s", "ENABLED" if tracer.enabled else "DISABLED")
```

同时在 `HarnessAPI.__init__()` 中（`harness/api/deps.py`）添加 `self.tracer = None` 属性。

#### 1.5 验收

运行 `uv run pytest -x` 确认 341 项测试全部通过。日志中应出现 `Langfuse tracing: ENABLED`。

---

### Phase 2: Scheduler 埋点

#### 2.1 修改 `harness/core/scheduler/base.py`

- `BaseScheduler.__init__()` 新增参数 `tracer: LangfuseTracer | None = None`
- 添加 `self.tracer = tracer`

#### 2.2 修改 `harness/api/deps.py` — `start_run()` 方法

创建 Planner/Scheduler 时传入 `tracer=self.tracer`：

```python
scheduler = PlanningExecutorScheduler(
    ...,
    tracer=self.tracer,  # 新增
)
```

#### 2.3 修改 `harness/core/scheduler/loop.py`

在 `_run_loop()` 方法中：

```python
async def _run_loop(self, run_id: str, intent: str) -> RunState:
    # 1. 创建 trace
    trace_ctx = None
    if self.tracer and self.tracer.enabled:
        trace_ctx = self.tracer.start_run(run_id, intent, "serial")

    await self._ensure_run_started(run_id, intent)

    for _iteration in range(1, self.config.max_iterations + 1):
        # 2. 创建迭代 span
        iter_ctx = None
        if trace_ctx and self.tracer.enabled:
            iter_ctx = self.tracer.start_iteration(trace_ctx, _iteration)

        # ... 原有逻辑 ...

        # 3. 结束迭代 span
        if iter_ctx:
            self.tracer.end_iteration(iter_ctx)

    # 4. 结束 trace
    if trace_ctx:
        self.tracer.end_run(trace_ctx, final_state.status.value, ...)
```

#### 2.4 修改 `harness/core/scheduler/plan.py`

类似 `loop.py`，额外记录 `PlanCreated` 和 `PlanRevised` 事件（使用 `tracer.trace_event()`）。

#### 2.5 验收

模拟一次 Agent Run，在 Langfuse Dashboard 中能看到 Run → Iteration 的层级 trace。

---

### Phase 3: LLM + Tool 埋点

#### 3.1 修改 `harness/core/llm_client.py`

在 `OpenAILLMClient.chat()` 方法中，调用前后记录 LLM generation：

```python
async def chat(self, messages, tools, temperature, max_tokens):
    _t0 = time.monotonic()

    # ... 原有 httpx 调用 ...

    _ms = (time.monotonic() - _t0) * 1000
    usage = data.get("usage", {})

    # ── Langfuse tracing ──
    # 获取当前活跃的 TraceContext（通过 contextvars 传递）
    tracer = _get_current_tracer()
    ctx = _get_current_trace_ctx()
    if tracer and ctx and tracer.enabled:
        tool_call_names = [tc.name for tc in tool_calls]
        tracer.trace_llm_generation(
            ctx=ctx,
            model=self.model,
            messages=messages,
            response_content=content,
            tool_calls=tool_call_names,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            duration_ms=_ms,
        )

    return ChatResponse(...)
```

> **关键**：需要通过 `contextvars` 在同一次请求内传递 `TraceContext`。在 scheduler 中 `self.tracer` 可设置 contextvars，让深层调用（LLM client、Tool executor）能读取当前活跃的 trace/span。

#### 3.2 修改 `harness/tools/executor.py`

在 `ToolExecutor.execute()` 的 8 步流程中埋点：

- Guardrail 拦截后 → `tracer.trace_event("guardrail_blocked", level="WARNING", metadata={...})`
- 幂等命中 → `tracer.trace_tool_execution(..., cached=True)`
- 确认等待 → `tracer.trace_event("confirmation_needed", ...)`
- 工具执行成功 → `tracer.trace_tool_execution(..., status="completed", ...)`
- 工具失败/超时 → `tracer.trace_tool_execution(..., status="failed/timeout", ...)`

#### 3.3 验收

完整 Agent Run 后在 Langfuse 看到完整的 generation + span + event 树。

---

### Phase 4: 评测管道（新增 `evaluation/` 目录）

创建以下文件：

| 文件 | 说明 |
|------|------|
| `evaluation/__init__.py` | 空或简单导出 |
| `evaluation/datasets/__init__.py` | 空 |
| `evaluation/datasets/base.py` | `EvalCase` dataclass + `DatasetLoader`（从 YAML 加载） |
| `evaluation/datasets/jagent_eval.yaml` | 评测用例（见下方） |
| `evaluation/scorers/__init__.py` | 空 |
| `evaluation/scorers/rule_based.py` | 规则评分器 |
| `evaluation/scorers/llm_judge.py` | LLM-as-Judge 评分器 |
| `evaluation/run_eval.py` | 评测入口脚本 |

数据集设计参考 `JAgent-docs/Dev/LANGFUSE_INTEGRATION_PLAN.md` 第 8 节。

`run_eval.py` 支持以下参数：
- `--dataset` — 数据集路径
- `--scenario` — 按场景筛选（可选）
- `--case-id` — 按 ID 筛选（可选）

评测流程：对每条用例 → 调用 JAgent API 发起 run → 等待完成 → 运行评分器 → 写入 score。

#### 验收

运行 `uv run python evaluation/run_eval.py --dataset evaluation/datasets/jagent_eval.yaml --scenario "单步工具调用"` 后在 Langfuse Dashboard 看到对应的 trace + score。

---

### Phase 5: 验证

```bash
# 1. 回归测试
uv run pytest -x

# 2. 格式化 + lint
uv run ruff check harness/ evaluation/

# 3. 端到端验证：启动服务，发起一次 Agent Run
uv run python -m harness.api.serve &
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"intent": "读取 README.md 并总结前10行"}'

# 4. 在 https://jp.cloud.langfuse.com 确认 trace 可见
```

---

## 编码规范

### Python 风格
- Python 3.11+，使用 `from __future__ import annotations`
- 类型注解使用新语法（`list[dict]` 而非 `List[Dict]`）
- 行宽 120 字符（ruff 配置）
- 遵循现有代码的日志模式：使用 `harness.core.logger` 中的 logger
- **不要添加中文注释**——跟随项目现有代码风格（项目代码是英文注释）

### Langfuse SDK 使用
- Langfuse Python SDK v2.x，使用 `from langfuse import Langfuse` 导入
- Langfuse SDK 的 `flush()` 是同步的，通过 `asyncio.to_thread()` 异步化
- 不需要调用 Langfuse 的 `@observe()` 装饰器——使用手动 span 创建以精确控制层级

### 架构注意事项
- **不要**在受信组件中引入 LLM 推理
- **不要**让 Agent Kernel 感知 Langfuse 的存在
- Tracer 的启用/禁用状态不影响任何业务逻辑分支
- 所有 awaitable 方法必须是 `async def`

---

## 项目环境

- **包管理器**：`uv`（`uv sync` 安装依赖，`uv run` 执行脚本）
- **测试**：`uv run pytest`（asyncio_mode=auto）
- **Lint**：`uv run ruff check`
- **启动服务**：`uv run python -m harness.api.serve`

---

## 参考文档

详细的架构设计和评测方案见：
- `JAgent-docs/Dev/LANGFUSE_INTEGRATION_PLAN.md` — 完整方案文档
- `JAgent-docs/Dev/ARCHITECTURE_v2.1.md` — 项目架构
- `AGENTS.md` — 开发协作规范
