# Execution Data Layer 设计文档

> **版本**: v1.0
> **基线**: V0.7 Planner-Executor + DAG
> **关联文档**: `ARCHITECTURE_v2.1.md`, `harness_v2.1.md`, `TODO_v2.1.md`
> **最后更新**: 2026-06-07

---

## 1. 背景与问题

### 1.1 触发性 Bug

用户查询 5 个城市的天气，Agent 通过 `http_request` 正确获取了全部数据（Event Store 中 5 条 TOOL_COMPLETED 均有完整 JSON），但最终回答为 **"Hello! How can I help you today?"**——LLM 默认问候语。

### 1.2 根因分析

```
工具执行结果
    │
    ▼
DagExecutor._execute_step_only()
    │ output (完整) ──────────────→ results dict (Scheduler 持有)
    │ output_summary (200 chars) ─→ DAG_STEP_COMPLETED 事件
    │                                │
    ▼                                ▼
                         ContextManager.compress()
                              │ 逐条截断 300 chars
                              ▼
                         EpisodeSummary.key_findings = [] (空)
                              │
                              ▼
                         generate_answer()
                              │ 只读 state.summary
                              ▼
                        "Hello! How can I help you today?"
```

**双重架构失败**:

| 层次 | 问题 | 直接后果 |
|------|------|----------|
| **Planner** | `_PLAN_PROMPT` 示例引导 Agent 用 `file_op` 写文件作为"交付答案"的方式 | Agent 不产生自然语言回答文本 |
| **generate_answer** | 只读 `state.summary` (压缩后的 EpisodeSummary)，不读 `state.tool_results` (完整数据) | 压缩摘要为空时 LLM 无上下文 → 输出默认问候语 |

### 1.3 此前已否决的方案

| 方案 | 结论 | 原因 |
|------|------|------|
| 增大截断阈值 300→800 chars | ❌ 治标不治本 | 阈值再大也会爆，且不解决架构问题 |
| 中间截断保留首尾 | ❌ 同样治标 | 结构化的 JSON 中间截断后不可解析 |
| 直接读 `state.tool_results` | ✅ 可行但不够抽象 | 隐式耦合 `fold_events` 的实现细节 |

### 1.4 设计目标

1. **Agent 不规划展示步骤** — Planner 只规划数据获取，不规划 `file_op` 写文件
2. **最终回答使用完整数据** — 不经压缩层截断，直读原始工具输出
3. **解耦数据源** — 回答生成不依赖 Event Store 的折叠产物
4. **可扩展** — 支持多模态、大对象、流式、跨 Agent 数据共享

---

## 2. 设计哲学：从"数据管道"到"执行态数据层"

初始设计将 ExecutionDataPool 定位为"给 generate_answer 喂数据的管道"，这是**补丁思维**。正确的定位是：

### 2.1 两种数据存储的平行职责

```
┌─────────────────────────────────────┐   ┌─────────────────────────────────────┐
│          Event Store                │   │       Execution Data Layer          │
│        (What happened)              │   │     (What was produced)             │
│  ──────────────────────────         │   │  ──────────────────────────          │
│  审计 / 回放 / 调试                  │   │  消费 / 分析 / 衍生                   │
│  不可变 / 追加式 / 全量历史           │   │  按需转换 / 缓存 / 当前有效数据         │
│                                     │   │                                     │
│  事件类型:                           │   │  数据结构:                           │
│  TOOL_CALLED                        │   │  ExecutionEntry (带拓扑字段)          │
│  TOOL_COMPLETED                     │   │  Key: (run_id, tool_call_id)         │
│  TOOL_FAILED                        │   │  Value: output, status, error        │
│  ...                                │   │                                      │
└─────────────────────────────────────┘   └─────────────────────────────────────┘
         ▲                                        ▲
         │                                        │
         │  ToolExecutor.execute()                │ ToolExecutor 写基础字段
         │  (两处同时写入)                          │ DagExecutor enrich 拓扑
         │                                        │
         └──────────── ToolExecutor ──────────────┘
```

**Event Store 回答"Agent 做了什么"，Data Layer 回答"Agent 拿到了什么数据，现在能怎么用"。**

### 2.2 特权阶段：AnswerGenerator 绕过压缩层

最终回答生成是"特权阶段"——不经过 ContextManager 的压缩/截断，直接从 DataPool 读完整数据。

```
执行循环内:   Tool → Event Store → ContextManager.compress() → Agent 下一轮推理
                    ↑ 受压缩影响，但 Agent 不需要完整数据做决策

特权阶段:     DataPool.get_all(run_id) → AnswerGenerator → LLM → 最终回答
                    ↑ 不受压缩影响，需要完整数据
```

---

## 3. 整体技术架构图（更新版）

```
                          ┌─ 用户 / API ─┐
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                           Interface Layer                               │
│  REST: /api/v1/runs  │  WS: /api/v1/runs/{id}/events  │  Confirm/Pause  │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼───────────────────────────────────────┐
│                      Scheduler Layer (受信)                              │
│                                                                          │
│  ┌──────────────────────┐   ┌────────────────────────────────────────┐   │
│  │  AgentLoopScheduler  │   │  PlanningExecutorScheduler             │   │
│  │  (串行, fallback)     │   │  (Plan → Execute → Revise)            │   │
│  └──────────────────────┘   └────────────────────────────────────────┘   │
│                                   │                                       │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Planner (非受信)    │  DagExecutor (受信)  │  PlanGuardrail     │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                   │                                       │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  ContextManager (受信, 执行循环内压缩)  │  RunMonitor (受信)      │    │
│  └──────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼───────────────────────────────────────┐
│                         Tool Layer (受信)                                │
│                                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  ToolExecutor  │  │  Guardrail   │  │  Idempotency  │  │   Sandbox    │  │
│  │  8-step flow   │  │  Runner      │  │  Key Gen     │  │   (隔离)     │  │
│  └───────┬──────┘  └──────────────┘  └──────────────┘  └──────────────┘  │
│          │ 写 TOOL_COMPLETED / FAILED / TIMEOUT                           │
│          │ 同时写 DataPool (基础字段)                                     │
└──────────┼───────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     Storage Layer (持久 + 运行时)                         │
│                                                                          │
│  ┌──────────────────────────────┐  ┌──────────────────────────────────┐  │
│  │       Event Store            │  │     Execution Data Layer         │  │
│  │  (Append-Only, 审计/回放)     │  │  (Runtime, 消费/衍生)            │  │
│  │                              │  │                                  │  │
│  │  SQLite / PostgreSQL+JSONB   │  │  ┌────────────────────────────┐  │  │
│  │  on_append → WS + Monitor   │  │  │  DataPoolBackend (Protocol) │  │  │
│  └──────────────────────────────┘  │  │  ├─ InMemoryBackend (默认)  │  │  │
│                                    │  │  ├─ RedisBackend (未来)     │  │  │
│                                    │  │  └─ S3Backend (未来)       │  │  │
│                                    │  └────────────────────────────┘  │  │
│                                    │                                  │  │
│  ┌──────────────────────────────┐  │  ┌────────────────────────────┐  │  │
│  │   Analysis DB (持久化分析)    │  │  │  ExecutionEntry:           │  │  │
│  │   V1.0 分析平台               │  │  │  ├─ output/status/error   │  │  │
│  └──────────────────────────────┘  │  │  ├─ step_id/parent_ids    │  │  │
│                                    │  │  ├─ iteration/derived_from │  │  │
│                                    │  │  └─ scope/ to_snapshot()  │  │  │
│                                    │  └────────────────────────────┘  │  │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│               Answer Generation (特权阶段, 不走压缩层)                    │
│                                                                          │
│  ┌──────────────────────────────┐  ┌──────────────────────────────────┐  │
│  │    AnswerGenerator           │  │  OutputAdapter Registry          │  │
│  │                              │  │                                  │  │
│  │  1. pool.get_all(run_id)     │  │  DefaultTextAdapter (兜底)       │  │
│  │  2. estimate_tokens()       │  │  JsonMarkdownAdapter (结构化)    │  │
│  │  3. ≤ budget → 单调用        │  │  ImageAdapter (多模态, 未来)     │  │
│  │  4. > budget → 智能摘要+再调  │  │  CodeBlockAdapter (代码, 未来)  │  │
│  └──────────────────────────────┘  └──────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 4. 组件规格

### 4.1 DataPoolBackend（存储接口）

```python
# harness/core/data_pool.py

@runtime_checkable
class DataPoolBackend(Protocol):
    """存储后端抽象。

    今天用 InMemoryBackend，明天可换 Redis / S3 / SQLite。
    接口只关注数据存取，不关注业务语义。
    """

    def store(self, run_id: str, entry: ExecutionEntry) -> None: ...
    def get_all(self, run_id: str) -> list[ExecutionEntry]: ...
    def query(
        self, run_id: str,
        *, tool_name: str | None = None, status: str | None = None,
    ) -> list[ExecutionEntry]: ...
    def list_run_ids(self) -> list[str]: ...
    def clear(self, run_id: str) -> None: ...
    def snapshot(self, run_id: str) -> bytes: ...
```

### 4.2 InMemoryBackend（默认实现）

```python
class InMemoryBackend:
    """默认后端，进程内内存存储。

    约束:
      - max_runs: 最多保留 100 个 run 的数据（LRU 淘汰）
      - max_entry_size_mb: 单条 entry 超过此阈值时 output 被 offload
    """

    def __init__(self, max_runs: int = 100, max_entry_size_mb: float = 50):
        self._data: OrderedDict[str, list[ExecutionEntry]] = OrderedDict()
        self._max_runs = max_runs
        self._max_entry_size = max_entry_size_mb * 1024 * 1024
        self._large_objects: dict[str, str] = {}  # tool_call_id → file_path

    def store(self, run_id: str, entry: ExecutionEntry) -> None:
        self._evict_if_needed()
        if run_id not in self._data:
            self._data[run_id] = []
            self._data.move_to_end(run_id)
        if self._is_large(entry.output):
            entry.output = self._offload(entry.tool_call_id, entry.output)
        self._data[run_id].append(entry)

    def get_all(self, run_id: str) -> list[ExecutionEntry]:
        entries = self._data.get(run_id, [])
        for e in entries:
            if isinstance(e.output, str) and e.output.startswith("@offload:"):
                e.output = self._restore(e.output)
        return entries

    def query(self, run_id, *, tool_name=None, status=None):
        entries = self.get_all(run_id)
        result = entries
        if tool_name:
            result = [e for e in result if e.tool_name == tool_name]
        if status:
            result = [e for e in result if e.status == status]
        return result

    def list_run_ids(self) -> list[str]:
        return list(self._data.keys())

    def clear(self, run_id: str) -> None:
        self._pop_large_objects(run_id)
        self._data.pop(run_id, None)

    def snapshot(self, run_id: str) -> bytes:
        """序列化整个 run 的 entry 列表，供跨进程 / 前端消费。"""
        import pickle
        return pickle.dumps(self._data.get(run_id, []))

    def _evict_if_needed(self) -> None:
        while len(self._data) >= self._max_runs:
            self._data.popitem(last=False)

    def _is_large(self, output: object) -> bool:
        return isinstance(output, (bytes, bytearray)) and len(output) > self._max_entry_size

    def _offload(self, tool_call_id: str, output: object) -> str:
        path = f".data_pool_offload/{tool_call_id}.bin"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(output if isinstance(output, bytes) else str(output).encode())
        return f"@offload:{path}"

    def _restore(self, ref: str) -> object:
        path = ref[len("@offload:"):]
        with open(path, "rb") as f:
            return f.read()
```

### 4.3 ExecutionEntry（数据单元）

```python
@dataclass
class ExecutionEntry:
    # ── 基础字段（由 ToolExecutor 写入） ──
    tool_call_id: str
    tool_name: str
    status: str                        # "completed" | "failed" | "timeout" | "guardrail_blocked"
    output: Any = None
    error: str | None = None
    duration_ms: int = 0

    # ── 拓扑字段（由 DagExecutor enrich） ──
    step_id: str = ""                  # 对应 Plan 中的 step.id
    parent_step_ids: list[str] = field(default_factory=list)  # 数据依赖关系
    iteration: int = 0                 # PlanRevised 后的执行轮次
    derived_from: list[str] = field(default_factory=list)     # 数据血缘

    # ── 跨 Agent 字段（预留） ──
    scope: str = "private"             # "private" | "shared" | "broadcast"

    def to_snapshot(self) -> dict:
        """供前端 / 流式消费，不含 output 大对象。"""
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "step_id": self.step_id,
            "iteration": self.iteration,
            "duration_ms": self.duration_ms,
            "has_output": self.output is not None,
            "has_error": self.error is not None,
        }
```

### 4.4 ExecutionDataPool（对外门面）

```python
class ExecutionDataPool:
    """执行数据池——Agent 系统的运行时数据层。

    职责:
      - 向 ToolExecutor 提供 store() 入口（必选）
      - 向 DagExecutor 提供 update_metadata() 入口（可选 enrich）
      - 向 AnswerGenerator 提供 get_all()/query() 入口（消费）
      - 向外部提供 clear() 入口（显式清理）

    不负责:
      - 持久化（委托给 backend）
      - 数据变换（委托给 OutputAdapter）
      - 智能摘要（委托给 AnswerGenerator）
    """

    def __init__(self, backend: DataPoolBackend | None = None):
        self._backend = backend or InMemoryBackend()

    # ── 写入 ────────────────────────────────────────

    def store(self, run_id: str, entry: ExecutionEntry) -> None:
        self._backend.store(run_id, entry)

    def update_metadata(
        self, run_id: str, tool_call_id: str, **kwargs,
    ) -> bool:
        """补充拓扑信息（step_id, parent_step_ids, iteration 等）。

        由 DagExecutor 在写完 DAG_STEP_COMPLETED 后调用。
        按 tool_call_id 查找（倒序，最近优先），找到即赋值。

        Returns:
            True 表示找到并更新了 entry
            False 表示未找到（可能是幂等命中，entry 由之前写入）
        """
        entries = self._backend.get_all(run_id)  # 后端可能返回副本
        for entry in reversed(entries):
            if entry.tool_call_id == tool_call_id:
                for k, v in kwargs.items():
                    setattr(entry, k, v)
                return True
        return False

    # ── 读取 ────────────────────────────────────────

    def get_all(self, run_id: str) -> list[ExecutionEntry]:
        return self._backend.get_all(run_id)

    def query(self, run_id: str, **filters) -> list[ExecutionEntry]:
        return self._backend.query(run_id, **filters)

    # ── 管理 ────────────────────────────────────────

    def list_run_ids(self) -> list[str]:
        return self._backend.list_run_ids()

    def clear(self, run_id: str) -> None:
        """显式清理——用户倾向于保留条目供查询，不自动 clear。"""
        self._backend.clear(run_id)

    def snapshot(self, run_id: str) -> bytes:
        return self._backend.snapshot(run_id)
```

### 4.5 OutputAdapter（消费适配器接口）

```python
# harness/core/answer_generator.py (或 data_pool_formatters.py)

class OutputAdapter(Protocol):
    """将一组同类型 tool 的 ExecutionEntry 转为 LLM 可消费的上下文。

    不同工具的输出需要不同的转换策略:
      - http_request (JSON) → 结构化文本 / Markdown 表格
      - browser (截图 base64) → 多模态 image_url
      - run_code (代码) → 代码块 + 智能截断
    """

    def to_llm_context(
        self, entries: list[ExecutionEntry], budget_tokens: int,
    ) -> str | list[dict]:
        ...


class DefaultTextAdapter:
    """兜底适配器：JSON → 缩进文本，非 JSON → str()。"""

    def to_llm_context(
        self, entries: list[ExecutionEntry], budget_tokens: int,
    ) -> str:
        parts = []
        for e in entries:
            header = f"[{e.tool_name}] {'✓' if e.status == 'completed' else '✗'}"
            if e.output is not None:
                text = (
                    json.dumps(e.output, ensure_ascii=False, indent=2)
                    if not isinstance(e.output, str)
                    else e.output
                )
                parts.append(f"{header}\n{text}")
            elif e.error:
                parts.append(f"{header}\nERROR: {e.error}")
        return "\n\n".join(parts)


class OutputAdapterRegistry:
    """按 tool_name 注册/解析适配器。"""

    def __init__(self):
        self._adapters: dict[str, OutputAdapter] = {}
        self._default: OutputAdapter = DefaultTextAdapter()

    def register(self, tool_name: str, adapter: OutputAdapter) -> None:
        self._adapters[tool_name] = adapter

    def resolve(self, tool_name: str) -> OutputAdapter:
        return self._adapters.get(tool_name, self._default)
```

### 4.6 AnswerGenerator（特权阶段生成器）

```python
class AnswerGenerator:
    """终局回答生成器——不经过 ContextManager，直读 DataPool。

    职责:
      - 从 DataPool 获取完整工具执行结果
      - 智能判断是否需要分段摘要（防上下文溢出）
      - 调用 LLM 生成自然语言回答

    特权阶段:
      所有方法均不调用 context_manager.maybe_compress() 或类似截断逻辑。
    """

    def __init__(
        self,
        llm_client: LLMClient,
        data_pool: ExecutionDataPool,
        adapter_registry: OutputAdapterRegistry | None = None,
        context_budget_tokens: int = 4000,
    ):
        self.llm = llm_client
        self.pool = data_pool
        self.adapters = adapter_registry or OutputAdapterRegistry()
        self.budget = context_budget_tokens

    async def generate(
        self, run_id: str, query: str, feedback: str | None = None,
    ) -> str:
        """生成最终回答。

        流程:
          1. 从 DataPool 获取全部 entries
          2. 按 tool_name 分组 → 各 adapter 转换
          3. 估算 token → 超预算则智能摘要
          4. 调 LLM → 返回回答

        不走 context_manager.compress()，不受执行期压缩策略影响。
        """
        entries = self.pool.get_all(run_id)
        if not entries:
            return await self._direct_answer(query, feedback)

        # ── 按工具分组，各 adapter 转换 ──
        context_parts = []
        for tool_name, group in self._group_by_tool(entries):
            adapter = self.adapters.resolve(tool_name)
            context_parts.append(
                adapter.to_llm_context(list(group), self.budget // max(len(group), 1))
            )
        full_context = "\n\n".join(context_parts)

        # ── token 估算 → 决定单调还是摘要+再调 ──
        estimated = self._estimate_tokens(full_context)
        if estimated <= self.budget:
            return await self._direct_answer(query, feedback, context=full_context)
        else:
            summary = await self._smart_summarize(full_context)
            return await self._direct_answer(query, feedback, context=summary)

    async def _direct_answer(
        self, query: str, feedback: str | None, context: str | None = None,
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. Answer the user's question "
                    "directly and naturally based on the tool results below.\n"
                    "Do not call any tools.\n"
                ),
            },
        ]
        if feedback:
            messages.append({"role": "system", "content": f"## Feedback\n{feedback}"})
        if context:
            messages.append({"role": "user", "content": f"## Tool Results\n{context}"})
        messages.append({"role": "user", "content": query})
        response = await self.llm.chat(messages, temperature=0.7, max_tokens=1024)
        return response.strip()

    async def _smart_summarize(self, context: str) -> str:
        """LLM 驱动的智能摘要——保留关键数据点，大幅降低 token。

        这是语义压缩（"summarize keeping all specific values"），
        不是执行期截断（"truncate to 300 chars"）。
        """
        prompt = (
            "You are a data summarizer. Condense the following tool execution "
            "results into a concise summary (under 2000 chars).\n"
            "Preserve ALL specific data values, numbers, and key facts.\n"
            "Group similar entries, note patterns, highlight outliers.\n\n"
            f"Results:\n{context[:10000]}"  # 安全截断防止智能摘要自身爆上下文
        )
        return await self.llm.chat(
            [{"role": "system", "content": prompt}],
            temperature=0.0, max_tokens=1000,
        )

    @staticmethod
    def _group_by_tool(
        entries: list[ExecutionEntry],
    ) -> list[tuple[str, list[ExecutionEntry]]]:
        seen: dict[str, list[ExecutionEntry]] = {}
        for e in entries:
            seen.setdefault(e.tool_name, []).append(e)
        return list(seen.items())

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return len(text) // 2  # 粗略估算：2 chars ≈ 1 token
```

---

## 5. 注入与数据流

### 5.1 两阶段写入

```
阶段一: ToolExecutor.execute() 完成
───────────────────────────────────────────────
  TOOL_COMPLETED → pool.store(run_id, ExecutionEntry(
      tool_call_id, tool_name, status="completed",
      output=<完整数据>, duration_ms=...,
  ))
  TOOL_FAILED   → pool.store(run_id, ExecutionEntry(
      tool_call_id, tool_name, status="failed",
      error=..., duration_ms=...,
  ))
  TOOL_TIMEOUT  → pool.store(run_id, ExecutionEntry(
      tool_call_id, tool_name, status="timeout",
      error=..., duration_ms=...,
  ))
  GUARDRAIL     → pool.store(...status="guardrail_blocked"...)
  幂等命中      → 不写（数据已在首次执行时写入）

  写什么: output, status, error, duration_ms (ToolExecutor 当时能拿到的全部信息)
  不写什么: step_id, parent_step_ids (ToolExecutor 不知道 DAG 拓扑)

阶段二: DagExecutor._execute_layer() 处理结果
───────────────────────────────────────────────
  _execute_step_only() 返回:
    {"status":"completed","output":...,"tool_call_id":"...","duration_ms":...}
                                    ↑ 新增字段 (原返回值不含这两个字段)

  _execute_layer() 写 DAG_STEP_COMPLETED 事件后:
    pool.update_metadata(run_id, tool_call_id,
        step_id=step.id,
        parent_step_ids=step.dependencies,
        iteration=current_iteration,  # (可选，从 scheduler 传下)
    )

  写什么: step_id, parent_step_ids, iteration (DagExecutor 知道 DAG 拓扑)
  不写什么: output, status (已被阶段一写入，不改)
```

**为什么ToolExecutor不写拓扑**:
- ToolExecutor 的签名是 `execute(run_id, tool_name, input, tool_def, tool_fn)`——没有 `step_id`
- DagExecutor 有 `step.id`、`step.dependencies`——天然持有拓扑信息
- `update_metadata` 不是"脏操作"——它补充的是 ToolExecutor 层**客观上不存在的信息**

**为什么不在ToolExecutor加拓扑**:
- 会导致 ToolExecutor 依赖 DagPlan 的数据结构
- 串行路径（无 DAG）还需要传空值
- 两阶段写入是架构清晰的体现，不是设计缺陷

### 5.2 串行路径（AgentLoopScheduler）

串行路径不走 DagExecutor，entry 只由 ToolExecutor 写入（阶段一），不经过 `update_metadata`。

```
AgentLoopScheduler._run_tool_call()
  → executor.execute(...)
    → pool.store(run_id, ExecutionEntry(status, output, ...))
      ↑ step_id="" (默认值), parent_step_ids=[] (默认值)
```

串行路径没有 DAG 拓扑，`step_id=""` 语义正确。

### 5.3 AnswerGenerator 消费

```
最终回答触发
    │
    ▼
Scheduler._finalize_with_summary()
    │
    ├─ _refresh_state()  → RunState (用来写 event, 但不传给 generate)
    │
    └─ answer_generator.generate(run_id, intent, feedback)  ← 新的消费路径
         │
         ├─ pool.get_all(run_id)  ← 完整数据，不受压缩影响
         ├─ _group_by_tool + adapter.to_llm_context() ← 按类型适配
         ├─ _estimate_tokens() ← 判断 budget
         │
         ├─ ≤ budget → _direct_answer(context)
         │     └─ LLM prompt:
         │          System: "Answer based on tool results..."
         │          User:   "## Tool Results\n{context}"
         │          User:   "{intent}"
         │          → 自然语言回答
         │
         └─ > budget → _smart_summarize(context) → _direct_answer(summary)
               └─ 第一步: LLM 语义压缩（保留数据值）
                 第二步: LLM 生成最终回答
```

### 5.4 关键验证

| 场景 | Event Store | ExecutionDataPool | context_manager | 最终回答 |
|------|-------------|-------------------|-----------------|----------|
| 5 城天气 | 5 条 TOOL_COMPLETED | 5 个 entry (完整 JSON) | 不经过 | "北京22°C…" |
| 100 城天气 | 100 条 TOOL_COMPLETED | 100 个 entry (完整 JSON) | 不经过 | 智能摘要 → 回答 |
| 工具失败 | TOOL_FAILED | entry.status="failed" | 不经过 | "查询X城市失败" |
| 幂等命中 | 不写新事件 | 不写新 entry (已有的) | 不经过 | 同首次执行结果 |
| 串行路径 | 标准事件流 | entry.step_id="" | 不经过 | 正常回答 |

---

## 6. 实施计划

### 6.1 文件清单

| # | 文件 | 操作 | 核心改动 |
|---|------|------|----------|
| 1 | `harness/core/data_pool.py` | **NEW** | `DataPoolBackend` Protocol + `InMemoryBackend` + `ExecutionEntry` + `ExecutionDataPool` |
| 2 | `harness/core/answer_generator.py` | **NEW** | `OutputAdapter` Protocol + `DefaultTextAdapter` + `OutputAdapterRegistry` + `AnswerGenerator` |
| 3 | `harness/tools/executor.py` | EDIT | `__init__` 接受 `data_pool`；`execute()` 四分支写 pool |
| 4 | `harness/core/dag_executor.py` | EDIT | `_execute_step_only` 返回值加 `tool_call_id`+`duration_ms`；`_execute_layer` enrich 拓扑 |
| 5 | `harness/core/planner.py` | EDIT | `generate_answer` 标记 deprecated 保留 fallback |
| 6 | `harness/core/scheduler.py` | EDIT | `PlanningExecutorScheduler` 接受 `answer_generator`；`_generate_answer` 优先用 |
| 7 | `harness/api/serve.py` | EDIT | 创建 `ExecutionDataPool` + `AnswerGenerator` + `InMemoryBackend`；注入 executor / scheduler |
| 8 | `harness/api/deps.py` | EDIT | `HarnessAPI` 持有 pool 引用（供前端查询和显式清理） |
| 9 | `harness/api/routes/runs.py` | OPTIONAL | 新增 `GET /api/v1/runs/{id}/data` 端点，暴露 pool 快照 |

### 6.2 实施顺序

```
Step 1  ──── 创建 data_pool.py
              ├─ DataPoolBackend (Protocol)
              ├─ InMemoryBackend
              ├─ ExecutionEntry
              └─ ExecutionDataPool

Step 2  ──── 创建 answer_generator.py
              ├─ OutputAdapter (Protocol)
              ├─ DefaultTextAdapter
              ├─ OutputAdapterRegistry
              └─ AnswerGenerator

Step 3  ──── 修改 executor.py
              ├─ __init__ 加 data_pool 参数
              └─ execute() 四分支调 pool.store()

Step 4  ──── 修改 dag_executor.py
              ├─ _execute_step_only 返回值加 tool_call_id / duration_ms
              └─ _execute_layer 调 pool.update_metadata()

Step 5  ──── 修改 scheduler.py
              └─ PlanningExecutorScheduler 接受 answer_generator

Step 6  ──── 修改 serve.py + deps.py
              ├─ 创建 pool + ag
              └─ 注入

Step 7  ──── 测试
              ├─ test_data_pool.py  (NEW)
              ├─ test_answer_generator.py  (NEW)
              ├─ test_executor.py  (补充)
              └─ test_dag_executor.py  (补充)
```

### 6.3 测试策略

| 测试文件 | 测试点 |
|----------|--------|
| `test_data_pool.py` | InMemoryBackend CRUD / LRU 淘汰 / 大对象 offload / `update_metadata` 按 tool_call_id 查找 / 幂等不写 |
| `test_answer_generator.py` | `generate()` 直读 pool / budget 超限触发智能摘要 / context_manager 不参与 / 空 entries 回退 / feedback 注入 |
| `test_executor.py` | 4 个出口写 pool / 幂等命中不写 / `data_pool=None` 不崩溃（旧注入兼容） |
| `test_dag_executor.py` | `_execute_step_only` 返回新字段 / `_execute_layer` enrich 拓扑 / 串行路径 `step_id=""` |
| `test_scheduler.py` | `_generate_answer` 优先用 `answer_generator` / fallback 到 `planner.generate_answer` |

---

## 7. 向后兼容

| 组件 | 是否破坏 | 措施 |
|------|----------|------|
| ToolExecutor 构造函数 | 不破坏 | `data_pool` 可选参数，默认 `None`，不传则旧行为 |
| DagExecutor 返回值 | 不破坏 | `_execute_step_only` 返回 dict 仅新增字段，旧消费者忽略新字段 |
| Scheduler 构造函数 | 不破坏 | `answer_generator` 可选参数，默认 `None` |
| `Planner.generate_answer` | 不破坏 | 保留且标记 deprecated，新代码走 AnswerGenerator |
| 前端 API | 不破坏 | 新增 `GET /data` 端点可选，不影响现有 WS/REST |
| Event Store 事件格式 | 不破坏 | 无变更 |

---

## 8. 预留拓展点

| 拓展方向 | 现在做了什么 | 未来需要什么 |
|----------|-------------|-------------|
| **多模态** | `OutputAdapter` protocol + 预留 `ImageAdapter` | 实现 `to_llm_context` 返回 `list[dict]`（多模态消息格式） |
| **大对象 offload** | `InMemoryBackend._offload()` 写文件 | 换 S3/MinIO 后端；流式分块读取 |
| **分布式/多 Worker** | `DataPoolBackend` Protocol | 实现 `RedisBackend` / `gRPCBackend` |
| **Multi-Agent** | `ExecutionEntry.scope` 字段预留 | 实现 `scope="shared"` 的跨 Agent 订阅 |
| **流式 UI** | `ExecutionEntry.to_snapshot()` | 集成 WebSocket 推送；StreamingDataPoolBackend |
| **跨 Run 分析** | `DataPoolBackend.query()` + `list_run_ids()` | 实现 AnalysisService 消费 DataPool |
| **增量执行 (PlanRevised 复用)** | `ExecutionEntry.iteration` + `derived_from` | 实现依赖链分析：哪些 entry 可复用、哪些需重跑 |

---

## 9. 附录：与现有架构的关系

### 9.1 在受信边界中的位置

```
受信组件:
  Event Store        ── 不变
  Tool Layer         ── ToolExecutor 写入 pool（新增职责）
  DagExecutor        ── enrich 拓扑（新增职责）
  Context Manager    ── 不变（不受此设计影响）
  Scheduler          ── 接收 AnswerGenerator（新增入口）

非受信组件:
  Planner            ── generate_answer deprecated
  LLM (Agent Kernel) ── 不变

新组件:
  ExecutionDataPool  ── 受信（运行时数据，可丢弃重建）
  AnswerGenerator    ── 受信（只读 pool，不碰 Event Store）
  DataPoolBackend    ── 受信（存储接口）
```

### 9.2 与 Event Store 的关系

| 维度 | Event Store | ExecutionDataPool |
|------|-------------|-------------------|
| 写入方 | ToolExecutor | ToolExecutor (+ DagExecutor enrich) |
| 读取方 | Scheduler (fold)、Monitor、前端 (WS) | AnswerGenerator、前端 (Snapshot) |
| 持久化 | 是 (SQLite/PG) | 否 (内存，可选 snapshot) |
| 不可变 | 是 (Append-Only) | 是 (仅 append + enrich，不修改 output) |
| 可压缩 | 否 (全量保留) | 否 (全量保留直到 clear) |
| 拓扑 | 隐式 (seq 顺序) | 显式 (step_id, parent_step_ids) |

### 9.3 为什么不直接用 state.tool_results

| 对比项 | state.tool_results | ExecutionDataPool |
|--------|-------------------|-------------------|
| 数据来源 | fold_events() 副产品 | 显式写入 |
| 与 Event Store 耦合 | 紧——fold 改动就影响 | 松——独立协议 |
| 可查询 | 无（只能线性遍历） | 支持 query(tool_name, status) |
| 拓扑 | 无（平铺列表） | 有（step_id, parent_ids） |
| 大对象 | 无检测 | 有 offload 机制 |
| 跨进程 | 不可用 | 有 backend 抽象 |
| 可测试性 | 需构造复杂 RunState | 直接 mock backend |

---

*文档版本 v1.0 · 对应 V0.7 架构基线 · 执行态数据层设计*
