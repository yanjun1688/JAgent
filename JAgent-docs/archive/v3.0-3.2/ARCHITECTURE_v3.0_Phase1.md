# ARCHITECTURE v3.0 Phase 1: 上下文压缩与剪枝优化

> **版本**: v3.0 Phase 1
> **前置依赖**: Harness v2.1 V0.7.1（任务完成语义分层）+ V0.9（生命周期恢复）
> **基线**: 719 tests passed
> **状态**: 待审查

---

## 1. 目标与范围

### 1.1 目标

改造 `ContextManager` 的压缩与剪枝机制，使压缩后的上下文对 Agent **真正有用**：

1. 用精确 token 计数替代 `char × 0.25` 估算
2. 用结构化 `Episode` 作为统一摘要单元（不再保留独立的 `EpisodeSummary`）
3. 用重要性评分替代 seq 二分剪枝
4. 用分级压缩策略替代单一阈值触发

### 1.2 范围

**仅工作记忆层**（上下文窗口）内的压缩与剪枝：

- 改造 `harness/core/context_manager.py`
- 新增 `harness/core/token_counter.py`
- 扩展 `harness/models/events.py`
- 扩展 `harness/core/fold.py`
- 扩展 `harness/core/system_prompt.py`

**不做**（边界声明）：

- 不做语义记忆跨 Episode 提炼
- 不做向量嵌入生成和语义检索（`Episode.embedding` 预留但为 `None`）
- 不做 Agent 自主记忆工具
- 不做跨 Run 持久化记忆
- 不做前端 Episode 浏览 UI

---

## 2. 架构约束

本阶段严格遵循 Harness v2.1 的核心约束：

1. **EventStore 是唯一 truth source**。所有状态必须能从 EventStore 事件流折叠得到。
2. **不新增独立数据源**。MemoryStore / 索引层不是本阶段内容。
3. **Agent 不感知压缩机制**。压缩是系统基础设施行为。
4. **Event Store 保持 Append-Only**。剪枝只影响 `fold_events` 后的 `RunState` 内存状态，不删除原始事件。
5. **受信组件行为不依赖 Agent 配合**。重要性评分、压缩策略由系统强制决定。

---

## 3. 关键设计决策

| 决策点 | 方案 | 理由 |
|--------|------|------|
| Token 计数默认策略 | `tiktoken` 本地计数 | 无需外部 API key，开箱即用 |
| Token 计数升级路径 | `.env` 配置 `TOKEN_COUNTER_API_URL` + `TOKEN_COUNTER_API_KEY` 后自动切换远程 Provider | 用户拿到正式 API 后只需改配置 |
| Token 计数兜底 | `char × 0.25` 启发式 | tiktoken 不可用时保持系统可用 |
| Episode 与 EpisodeSummary | 删除 `EpisodeSummary`，`Episode` 直接作为唯一结构化摘要类型 | 无兼容别名，减少模型冗余 |
| ContextCompressed 事件 | 废弃写入，保留枚举和 fold 读取 | 新 Run 只写 `EpisodeArchived`，旧 `CONTEXT_COMPRESSED` 事件仍可读 |
| 惰性清理 | 写 `ContextPruned` 事件 | 保证 fold 一致性 |
| 压缩执行者 | 统一由 `ContextManager.maybe_compress()` 内部判断 | Scheduler 不感知策略细节 |

---

## 4. 事件 Schema 变更

### 4.1 新增事件类型

```python
class EventType(str, Enum):
    # ... 现有 43 种事件类型 ...
    EPISODE_ARCHIVED = "EpisodeArchived"
    CONTEXT_PRUNED = "ContextPruned"
```

### 4.2 新增/改造 Payload 模型

#### `Episode`

```python
class Episode(BaseModel):
    """Structured episode memory unit."""
    title: str
    summary: str
    importance_score: float = 0.0
    embedding: list[float] | None = None
    parent_episode_id: str | None = None
    format: str = "structured"  # "structured" | "legacy"
    episode_range: tuple[int, int]
    original_tokens: int
    compressed_tokens: int
    key_decisions: list[str]
    tools_used: list[str]
    key_findings: list[str]
    errors_encountered: list[str]
    current_plan: str | None
    original_event_refs: list[int]
```

> **注意**：v3.0 Phase 1 删除了独立的 `EpisodeSummary` 类型，`Episode` 直接包含全部字段。旧代码中 `EpisodeSummary` 的引用由 `Episode` 替代；`harness/__init__.py` 不再导出 `EpisodeSummary`。

#### `EpisodeArchivedPayload`

```python
class EpisodeArchivedPayload(BaseModel):
    original_tokens: int
    compressed_tokens: int
    episode: Episode
    keep_recent_count: int
    archived_event_refs: list[int]
```

#### `ContextPrunedPayload`

```python
class ContextPrunedPayload(BaseModel):
    pruned_event_refs: list[int]
    pruned_token_count: int
    pruned_seq_count: int
    reason: str = "lazy_clear"
```

### 4.3 废弃但不删除的事件

- `EventType.CONTEXT_COMPRESSED` 保留在枚举中
- `ContextCompressedPayload` 保留在 `PAYLOAD_MODEL_MAP` 中
- 新代码不再写入 `CONTEXT_COMPRESSED`
- `fold.py` 保留 `CONTEXT_COMPRESSED` 分支以读取历史事件

---

## 5. TokenCounter 设计

### 5.1 抽象接口

```python
# harness/core/token_counter.py

from abc import ABC, abstractmethod


class TokenCounter(ABC):
    """Abstract token counter with auto-fallback chain."""

    @abstractmethod
    async def count(self, text: str) -> int: ...

    async def count_messages(self, messages: list[dict]) -> int:
        return sum(await self.count(str(m.get("content", ""))) for m in messages)
```

### 5.2 三级实现

```python
class ProviderTokenCounter(TokenCounter):
    """Remote tokenize API implementation.

    Configured via .env:
      TOKEN_COUNTER_API_URL=https://api.example.com/v1/tokenize
      TOKEN_COUNTER_API_KEY=sk-xxx
      TOKEN_COUNTER_MODEL=qwen3.7-max

    Default request/response format (OpenAI-compatible):
      POST {url}
      Authorization: Bearer {key}
      Body: {"model": "...", "input": "..."}
      Response: {"tokens": 123}

    Users can subclass and override _call_api() for vendor-specific formats.
    """


class TiktokenTokenCounter(TokenCounter):
    """Local tiktoken implementation. Default for Phase 1."""

    def __init__(self, model: str = "cl100k_base"):
        import tiktoken
        self.encoding = tiktoken.get_encoding(model)

    async def count(self, text: str) -> int:
        return len(self.encoding.encode(text or ""))


class HeuristicTokenCounter(TokenCounter):
    """Fallback when tiktoken is unavailable: char_count * 0.25."""

    async def count(self, text: str) -> int:
        return max(1, int(len(text or "") * 0.25))
```

### 5.3 工厂函数

```python
def create_token_counter(
    strategy: str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> TokenCounter:
    """Create token counter with automatic fallback.

    Initialization-time resolution:
      1. If strategy == "provider" or (api_url and api_key): ProviderTokenCounter
      2. If strategy == "tiktoken" or tiktoken available: TiktokenTokenCounter
      3. HeuristicTokenCounter (final fallback)

    Runtime fallback: ProviderTokenCounter catches API failures and delegates
    to TiktokenTokenCounter / HeuristicTokenCounter transparently, so the
    caller never sees a counter failure.
    """
```

### 5.4 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `TOKEN_COUNTER_STRATEGY` | 强制策略：`auto` / `provider` / `tiktoken` / `heuristic` | `auto` |
| `TOKEN_COUNTER_API_URL` | Provider tokenize API endpoint | 无 |
| `TOKEN_COUNTER_API_KEY` | Provider API key | 无 |
| `TOKEN_COUNTER_MODEL` | tiktoken encoding 或 provider model | `cl100k_base` |

**使用示例**：

```bash
# 默认：本地 tiktoken，无需 API key
# （无需配置）

# 切换到远程 Provider：
TOKEN_COUNTER_STRATEGY=provider
TOKEN_COUNTER_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1/tokenize
TOKEN_COUNTER_API_KEY=sk-xxx
TOKEN_COUNTER_MODEL=qwen3.7-max

# 强制使用启发式：
TOKEN_COUNTER_STRATEGY=heuristic
```

### 5.5 与 LLM usage 的校验（可选，Phase 1 不强制）

`OpenAILLMClient.chat()` 已读取 `usage.prompt_tokens`。实现后可选：

- 在调试/测试中对比 `TokenCounter.count_messages()` 与 `usage.prompt_tokens`
- 记录偏差日志，用于校准 tiktoken encoding 选择
- 不阻塞主流程，不作为验收标准

---

## 6. ContextManager 重构

### 6.1 构造函数变更

```python
class ContextManager:
    def __init__(
        self,
        store,
        llm_client: LLMClient | None = None,
        token_counter: TokenCounter | None = None,
        token_limit: int = 128_000,
        checkpoint_interval: int = 10,
        lazy_clear_ratio: float = 0.5,
        compression_threshold_ratio: float = 0.7,
        emergency_threshold_ratio: float = 0.9,
    ):
```

**注意**：原 `compression_threshold_ratio=0.8` 调整为 `0.7`（情节归档阈值），新增 `lazy_clear_ratio=0.5`。

### 6.2 三层压缩策略

统一入口：`ContextManager.maybe_compress(run_id, iteration, state)`

```
token_count / token_limit:
    <= 0.5   → 不处理
    (0.5, 0.7] → lazy_clear()
    (0.7, 0.9] → archive_episode()
    > 0.9    → emergency_compact()
```

#### 惰性清理（Lazy Clear）

```python
async def _lazy_clear(self, run_id, state, token_count):
    # 1. 识别低重要性且已被 Agent 处理的事件
    pruned_refs = self._select_low_importance_events(state)
    # 2. 写入 ContextPruned 事件
    await self.store.append_event(
        run_id,
        EventType.CONTEXT_PRUNED,
        ContextPrunedPayload(...).model_dump(),
    )
```

**特点**：
- 不写 `EpisodeArchived`
- 只移除 `ToolResult` / `ThoughtEntry` 中的低重要性项
- 关键决策、错误、用户指令保留

#### 情节归档（Episode Archive）

```python
async def _archive_episode(self, run_id, state, token_count):
    # 1. 选择压缩窗口
    window = self._select_archive_window(state)
    # 2. LLM 生成结构化 Episode JSON
    episode = await self._generate_episode(state, window, token_count)
    # 3. 写入 EpisodeArchived 事件
    await self.store.append_event(
        run_id,
        EventType.EPISODE_ARCHIVED,
        EpisodeArchivedPayload(...).model_dump(),
    )
```

#### 紧急压缩（Emergency Compact）

```python
async def _emergency_compact(self, run_id, state, token_count):
    # 1. 压缩最旧的 50% 事件
    # 2. 忽略阶段边界和重要性评分
    # 3. 保留最近 3 轮
    # 4. 生成简化的 Episode（title/summary 优先）
```

### 6.3 重要性评分

```python
def _score_event_importance(self, event_or_entry) -> float:
    """System-enforced importance scoring, no LLM required."""
```

| 信息类型 | 重要性 |
|---------|--------|
| 用户指令（`RunStarted.intent`） | 0.9 |
| 错误事件（`ToolFailed` / `GuardrailTriggered`） | 0.8 |
| 关键决策（`PlanCreated` / `PlanRevised` / `DagStepCompleted`） | 0.7 |
| Agent thought（无特殊标记） | 0.5 |
| 工具成功输出（已处理） | 0.2 |
| 重复工具调用（相同 tool + input hash） | 0.1 |

### 6.4 Episode 生成 Prompt

扩展 `_SUMMARIZE_PROMPT`，要求 LLM 输出结构化 JSON：

```json
{
  "title": "一句话标题",
  "summary": "3-5句叙事摘要",
  "key_decisions": ["..."],
  "tools_used": ["..."],
  "key_findings": ["..."],
  "errors_encountered": ["..."],
  "current_plan": "..."
}
```

无 LLM 或解析失败时降级为 `format: "legacy"`，`current_plan` 存原始文本。

---

## 7. fold.py 扩展

### 7.1 RunState 变更

```python
@dataclass
class RunState:
    # ... 现有字段 ...
    summary: Episode | str | None = None
    episodes: list[Episode] = field(default_factory=list)  # 新增（为 Phase 2 预留，Phase 1 可选实现）
```

**说明**：`episodes` 字段在 Phase 1 可选。如果实现，只用于累积 Episode 元数据；如果不实现，Phase 2 再追加。但 `EpisodeArchived` 事件的 fold 分支必须能处理 `state.episodes` 存在的情况。

### 7.2 新增 fold 分支

#### `EPISODE_ARCHIVED`

```python
case EventType.EPISODE_ARCHIVED:
    p = EpisodeArchivedPayload(**event.payload)
    state.summary = p.episode
    state.episodes.append(p.episode)
    state.keep_recent_count = p.keep_recent_count
    # 按重要性从 thought_history / tool_results 中移除已归档事件
    self._trim_archived_state(state, p.archived_event_refs, p.keep_recent_count)
```

#### `CONTEXT_PRUNED`

```python
case EventType.CONTEXT_PRUNED:
    p = ContextPrunedPayload(**event.payload)
    # 从 thought_history / tool_results 中移除 pruned_event_refs
    self._trim_pruned_state(state, p.pruned_event_refs)
```

### 7.3 兼容性处理

- `CONTEXT_COMPRESSED` 分支保留只读兼容，但逻辑改为：
  - 如果 `summary_ref` 是 `Episode` → 走 Episode 路径（`state.summary = summary_ref`，并追加到 `state.episodes`）
  - 如果 `summary_ref` 是 `str` → 走 legacy 路径（`state.summary = summary_ref`）
- 新 Run 不再写入 `CONTEXT_COMPRESSED`
- `state.episodes` 仅在 Episode 路径中追加

---

## 8. 环境变量配置

新增以下环境变量（用户可加入 `.env`）：

```bash
# Token 计数策略
TOKEN_COUNTER_STRATEGY=auto          # auto | provider | tiktoken | heuristic
TOKEN_COUNTER_MODEL=cl100k_base      # tiktoken encoding 或 provider model

# Provider tokenize API（可选，有则启用）
TOKEN_COUNTER_API_URL=
TOKEN_COUNTER_API_KEY=
```

项目现有 `.env` 示例：

```bash
LLM_API_KEY=sk-xxx
LLM_MODEL_NAME=qwen3.7-max-2026-06-08
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# 新增（Phase 1）：
TOKEN_COUNTER_STRATEGY=auto
TOKEN_COUNTER_MODEL=cl100k_base
```

---

## 9. 向后兼容

| 场景 | 处理方案 |
|------|---------|
| 老 Run 的 `CONTEXT_COMPRESSED` 事件 | `fold.py` 保留读取逻辑 |
| 代码引用 `EpisodeSummary` | 删除该类型；调用方改用 `Episode`（`harness/__init__.py` 不再导出 `EpisodeSummary`） |
| 老 `ContextCompressedPayload.summary_ref` | 类型实际为 `Episode \| str`；fold 时识别 `Episode` 或纯文本 |
| 现有 `context_manager` 测试 | 全部保留，新增测试不破坏 |
| `ContextManager.__init__` 新增 `token_counter` 参数 | 默认 `None` → 自动创建 `AutoTokenCounter` |

---

## 10. 测试策略

### 10.1 单元测试（`tests/test_token_counter.py` 新增）

- `TiktokenTokenCounter` 基本计数
- `HeuristicTokenCounter` 兜底
- `AutoTokenCounter` 降级链路（provider 失败 → tiktoken → heuristic）
- 环境变量解析

### 10.2 单元测试（`tests/test_context_manager.py` 扩展）

- Token 计数调用 `TokenCounter` 而非 `char × 0.25`
- 三层压缩策略触发条件
- `EpisodeArchived` 事件生成
- `ContextPruned` 事件生成
- 重要性评分规则
- 无 LLM 时降级为 `format: "legacy"`

### 10.3 集成测试（`tests/test_context_manager.py` 扩展）

- 50 轮长任务不溢出
- fold 后 `state.episodes` 累积
- 老 `CONTEXT_COMPRESSED` 事件仍可 fold
- EventStore 原始事件不被删除

### 10.4 回归测试

- 全部现有 719 项测试通过
- 保留现有 `ContextCompressed` 相关测试以验证老事件兼容
- 新增 `EpisodeArchived` / `ContextPruned` 测试覆盖新路径

---

## 11. 实现顺序

| # | 任务 | 文件 | 说明 |
|---|------|------|------|
| 1 | TokenCounter 抽象 + 实现 | `harness/core/token_counter.py` | 先实现 tiktoken + heuristic + 接口 |
| 2 | Episode 模型扩展 | `harness/models/events.py` | 新增 Episode / EpisodeArchivedPayload / ContextPrunedPayload |
| 3 | SUMMARIZE prompt 扩展 | `harness/core/system_prompt.py` | 输出结构化 JSON |
| 4 | ContextManager 重构 | `harness/core/context_manager.py` | 三层压缩 + 重要性评分 + TokenCounter 注入 |
| 5 | fold.py 扩展 | `harness/core/fold.py` | 处理 EPISODE_ARCHIVED / CONTEXT_PRUNED |
| 6 | 顶层导出 | `harness/__init__.py` | 导出新类型 |
| 7 | 单元测试 | `tests/test_token_counter.py` | 新增 |
| 8 | 集成测试 | `tests/test_context_manager.py` | 扩展 |
| 9 | 回归测试 | 全部测试 | 确保 719+ tests 通过 |

---

## 12. 验收标准

| # | 标准 | 验证方式 |
|---|------|---------|
| AC-1 | tiktoken 计数误差 < 5% | 100 条中英混合样本对比 |
| AC-2 | 三级策略自动降级 | 模拟 provider 不可用 / tiktoken 不可用场景 |
| AC-3 | `.env` 配置 API 后自动切 provider | 手动验证 |
| AC-4 | Episode JSON 解析成功率 ≥ 95% | Schema 校验 |
| AC-5 | title 非空，key_decisions ≥ 1 | LLM 输出验证 |
| AC-6 | 关键决策/错误在剪枝后存在于最新 Episode 摘要 | 集成测试 |
| AC-7 | 50 轮长任务不因上下文溢出中断 | 端到端测试 |
| AC-8 | 现有 context_manager 测试不破坏 | 回归测试 |
| AC-9 | 惰性清理写入 `ContextPruned` 事件 | 查询 EventStore |
| AC-10 | 新 Run 只写 `EpisodeArchived`，不写 `ContextCompressed` | 查询 EventStore |
| AC-11 | 历史 `ContextCompressed` 事件仍可正确 fold | 回归测试 |
| AC-12 | EventStore 原始事件不因剪枝被删除 | 直接查 DB |

---

## 13. 已知风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| tiktoken encoding 与当前 LLM 不匹配 | 计数偏差 > 5% | 通过 LLM usage 持续校准；用户可配置 `TOKEN_COUNTER_MODEL` |
| Provider tokenize API 格式不统一 | 远程计数器实现困难 | 默认用 tiktoken；Provider 实现提供可覆盖的 `_call_api()` 钩子 |
| LLM 结构化 JSON 输出不稳定 | Episode 解析失败率高 | JSON parse 失败降级为 `format: "legacy"` |
| 三层压缩策略增加复杂度 | 调试困难 | 每个层级写独立日志和事件 |
| `state.episodes` 列表无限增长 | 单 Run 内存占用增加 | Phase 1 只累积 Episode 元数据（几 KB/条），不累积 embedding |

---

## 14. 与 Phase 2 的衔接

Phase 1 为 Phase 2 预留以下扩展点：

| Phase 1 设计 | Phase 2 扩展 |
|-------------|-------------|
| `Episode.embedding: None` | 填充 embedding，启用语义检索 |
| `state.episodes: list[Episode]` | 作为 MemoryStore 索引源 |
| `EpisodeArchived` 事件 | 订阅构建 MemoryStore |
| `TokenCounter` 抽象 | 精确控制检索注入的 token 预算 |
| `ContextPruned` 事件 | 减少检索噪音 |

---

*本文件基于 `PRD_v3.0_Phase1_上下文压缩与剪枝优化.md` 编写，对齐 `AGENTS.md` v2.1 受信边界约束。*
