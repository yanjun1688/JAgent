# 任务：V3.0 Phase 1 测试实现

## 角色

你是 JAgent 项目的测试工程师。你的任务是为 Phase 1（上下文压缩与剪枝优化）编写全部测试代码，使全量测试通过。

## 项目信息

- 工作目录：`D:\Project\JAgent`
- 测试框架：pytest + pytest-asyncio（auto mode）
- 运行命令：`python -m pytest tests/ -v`
- 代码风格：英文注释，Pydantic v2，async

## 当前状态

### 功能代码已完成

以下文件已实现 Phase 1 功能：

| 文件 | 变更 |
|------|------|
| `harness/core/token_counter.py` | **新增** — TokenCounter 抽象 + TiktokenTokenCounter + HeuristicTokenCounter + ProviderTokenCounter（带 `_degraded` 永久降级）+ `create_token_counter()` 工厂 |
| `harness/core/context_manager.py` | **重写** — 3 层压缩（lazy_clear → archive_episode → emergency_compact），全部写 `EPISODE_ARCHIVED` 或 `CONTEXT_PRUNED`，不再写 `CONTEXT_COMPRESSED`。Token 估算用 async `TokenCounter` |
| `harness/core/fold.py` | 新增 `EPISODE_ARCHIVED` / `CONTEXT_PRUNED` fold 分支，`RunState` 新增 `episodes: list[Episode]`，`summary` 类型改为 `Episode \| str \| None`；`CONTEXT_COMPRESSED` 保留只读兼容 |
| `harness/core/system_prompt.py` | SUMMARIZE prompt 增加 `title` / `summary` 字段 |
| `harness/core/agent_kernel.py` | 识别 `Episode` 类型，展示 title + summary |
| `harness/models/events.py` | 新增 `Episode`（合并原 `EpisodeSummary` 字段），`EpisodeArchivedPayload`, `ContextPrunedPayload`, `EventType.EPISODE_ARCHIVED`, `EventType.CONTEXT_PRUNED`；删除 `EpisodeSummary` 导出 |
| `harness/__init__.py` | 导出新类型 |

### 当前测试状态

- **695 passed**（排除 test_context_manager.py 和 test_context_window.py）
- **38 failed**（均为旧测试引用已删除的 sync 方法或旧事件类型）

### 旧测试失败原因（你需要修复）

**test_context_manager.py** 中的旧测试失败原因：

1. **引用已删除的 sync 方法**：
   - `_estimate_context_tokens()` → 已删除，现在是 async `_async_estimate_context_tokens()`
   - `_estimate_text_tokens()` → 已删除，现在是 async `_async_estimate_text_tokens()`
   - `select_compression_window()` → 已删除
   - `_generate_summary()` → 已删除，现在是 `_generate_episode()`

2. **检查旧事件类型**：
   - 旧测试检查 `EventType.CONTEXT_COMPRESSED` → 新代码写 `EventType.EPISODE_ARCHIVED`
   - 旧测试检查 `ContextCompressedPayload` → 新代码写 `EpisodeArchivedPayload`

3. **阈值计算变了**：
   - 旧测试基于 `char × 0.25` 设计 token_limit
   - 新代码用 tiktoken 计数，对重复文本（如 `"x" * 50`）计数显著偏低（~7 tokens vs 旧 ~12）
   - 需要调大测试数据或调小 token_limit

**test_context_window.py** 中的旧测试失败原因同上。

## 测试计划

严格按照 `D:\Project\JAgent\JAgent-docs\Test_Plan\TestPlan_Phase1_上下文压缩与剪枝优化.md` 执行。该文件包含完整的测试用例清单（约 100 项），分为：

- Section 3: TokenCounter 单元测试（TC-U1 ~ TC-U17）
- Section 4: 事件模型单元测试（EM-U1 ~ EM-U22）
- Section 5: 重要性评分单元测试（IS-U1 ~ IS-U10）
- Section 6: 三层压缩集成测试（CM-I1 ~ CM-I26）
- Section 7: EpisodeArchived 替代测试（RC-U1 ~ RC-U4）
- Section 8: fold 事件处理测试（FL-I1 ~ FL-I15）
- Section 9: E2E 长任务测试（E2E-1 ~ E2E-5）
- Section 10: 契约测试（CT-1 ~ CT-5）
- Section 11: 回归测试

## 实现细节（影响测试编写）

### ContextManager 关键行为

```python
# 构造函数
ContextManager(
    store,                                    # EventStore 或 None
    llm_client=None,                          # LLMClient 或 None
    token_counter=None,                       # TokenCounter 或 None（默认 create_token_counter()）
    token_limit=128_000,
    checkpoint_interval=10,
    compression_threshold_ratio=0.7,          # 情节归档阈值
    emergency_threshold_ratio=0.9,            # 紧急压缩阈值
    lazy_clear_ratio=0.5,                     # 惰性清理阈值
)
```

### 3 层压缩策略

```
ratio = token_count / token_limit

ratio <= 0.5  → 不处理
0.5 < ratio <= 0.7  → _lazy_clear()  → 写 CONTEXT_PRUNED
0.7 < ratio <= 0.9  → _archive_episode()  → 写 EPISODE_ARCHIVED
ratio > 0.9  → _emergency_compact()  → 写 EPISODE_ARCHIVED (keep_recent_count=3)
```

### _archive_episode 只归档非最近事件

```python
keep = 2
compress_thoughts = state.thought_history[:-keep] if len(state.thought_history) > keep else []
compress_results = state.tool_results[:-keep] if len(state.tool_results) > keep else []
```

### _score_event_importance 评分规则

| 类型 | 分数 |
|------|------|
| ThoughtEntry 含决策关键词（decided/choose/select/plan/strategy/approach） | 0.7 |
| ThoughtEntry 普通 | 0.5 |
| ToolResult status=failed/timeout/guardrail_blocked | 0.8 |
| ToolResult status=soft_error | 0.6 |
| ToolResult status=completed | 0.2 |
| 非 ThoughtEntry/ToolResult → raise TypeError | — |

### TokenCounter 降级行为

- `ProviderTokenCounter` 首次 API 失败后设 `_degraded=True`，永久降级到 fallback
- `create_token_counter()` 默认 auto：有 API 配置 → Provider，否则 → Tiktoken，tiktoken 不可用 → Heuristic

### Episode 模型

```python
class Episode(BaseModel):
    title: str               # 必填，_generate_episode 保证非空
    summary: str = ""
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

> 注意：`EpisodeSummary` 已删除，测试中应直接 import `Episode`。

### fold.py 新分支

- `EPISODE_ARCHIVED`：设 `state.summary = episode`，追加 `state.episodes`，按 `archived_event_refs` 截断 thought_history/tool_results（保留 keep_recent_count 最近轮）
- `CONTEXT_PRUNED`：按 `pruned_event_refs` 从 thought_history/tool_results 移除
- `CONTEXT_COMPRESSED`：保留只读兼容分支，不追加 `state.episodes`

## 执行步骤

### Step 1: 修复旧测试

**test_context_manager.py**：
- 删除或重写引用已删除方法的测试（`_estimate_context_tokens`, `select_compression_window`, `_generate_summary`）
- 将检查 `CONTEXT_COMPRESSED` 的测试改为检查 `EPISODE_ARCHIVED`
- 将 `EpisodeSummary` 导入/构造改为 `Episode`
- 调整 token_limit 和数据量使 tiktoken 计数能触发阈值
- 保留 `TestFindResumeSeq`、`TestCheckpoint` 等不受影响的测试

**test_context_window.py**：
- 同上处理

### Step 2: 按测试计划编写新测试

按 TestPlan 的 Section 3-10 顺序编写。每个测试用例 ID 必须与 TestPlan 对应。

### Step 3: 运行验证

```bash
# 全量测试
python -m pytest tests/ -v

# 单独验证各文件
python -m pytest tests/test_token_counter.py -v
python -m pytest tests/test_context_manager.py -v
python -m pytest tests/test_fold.py -v
python -m pytest tests/test_context_window.py -v
```

## 约束

1. **不得修改功能代码**。只改测试文件。
2. **回归基线**：排除测试文件自身的旧用例，其余 695 个测试必须继续通过。
3. **测试文件**：
   - `tests/test_token_counter.py` — 已存在，按 TestPlan Section 3 补充
   - `tests/test_context_manager.py` — 修复旧测试 + 按 TestPlan Section 5/6/7/9 补充
   - `tests/test_fold.py` — 按 TestPlan Section 8 补充
   - `tests/test_context_window.py` — 修复旧测试
4. **不依赖外部资源**：所有测试用 `:memory:` EventStore + MockLLMClient + MockAgentKernel
5. **pytest-asyncio auto mode**：所有 async 测试自动识别，无需 `@pytest.mark.asyncio`
6. **测试数据要用真实 ThoughtEntry/ToolResult**：不要用 `type("obj", ...)` mock，用 `from harness.core.fold import ThoughtEntry, ToolResult, ToolResultStatus`

## 关键 import 参考

```python
# 事件模型
from harness.models.events import (
    Episode, EpisodeArchivedPayload, ContextPrunedPayload,
    ContextCompressedPayload, ContextCheckpointedPayload,
    EventType, Event, PAYLOAD_MODEL_MAP,
)

# fold
from harness.core.fold import (
    RunState, RunStatus, ThoughtEntry, ToolResult, ToolResultStatus, fold_events,
)

# ContextManager
from harness.core.context_manager import ContextManager

# TokenCounter
from harness.core.token_counter import (
    TokenCounter, TiktokenTokenCounter, HeuristicTokenCounter,
    ProviderTokenCounter, create_token_counter,
)

# LLM
from harness.core.llm_client import MockLLMClient, ChatResponse

# Scheduler (for E2E)
from harness.core.scheduler import AgentLoopScheduler, SchedulerConfig
from harness import MockAgentKernel, ThinkResult, ToolExecutor, ToolDefinition, RetryPolicy, SideEffect, EventStore
```

## 验收标准

- [ ] `python -m pytest tests/ -v` 全部通过
- [ ] TestPlan 中 TC-U1~TC-U17 全部实现
- [ ] TestPlan 中 EM-U1~EM-U22 全部实现
- [ ] TestPlan 中 IS-U1~IS-U10 全部实现
- [ ] TestPlan 中 CM-I1~CM-I26 全部实现
- [ ] TestPlan 中 RC-U1~RC-U4 全部实现
- [ ] TestPlan 中 FL-I1~FL-I15 全部实现
- [ ] TestPlan 中 E2E-1~E2E-5 全部实现
- [ ] TestPlan 中 CT-1~CT-5 全部实现
- [ ] 原有 695 个非测试文件测试继续通过
