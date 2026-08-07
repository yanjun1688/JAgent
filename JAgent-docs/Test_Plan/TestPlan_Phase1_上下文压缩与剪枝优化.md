# TestPlan Phase 1: 上下文压缩与剪枝优化

> **版本**: 1.0
> **测试负责人**: Test Engineer
> **基线**: 719 tests passed
> **关联文档**: PRD_v3.0_Phase1 / ARCHITECTURE_v3.0_Phase1
> **测试策略**: 单元测试 → 集成测试 → 契约测试 → 回归测试
> **预估新增用例**: 55–65 项

---

## 目录

- [1. 测试策略总览](#1-测试策略总览)
- [2. 测试环境与基础设施](#2-测试环境与基础设施)
- [3. 单元测试 — TokenCounter](#3-单元测试--tokencounter)
- [4. 单元测试 — 事件模型](#4-单元测试--事件模型)
- [5. 单元测试 — 重要性评分与剪枝](#5-单元测试--重要性评分与剪枝)
- [6. 集成测试 — 三层压缩策略](#6-集成测试--三层压缩策略)
- [7. 集成测试 — EpisodeArchived 替代 ContextCompressed](#7-集成测试--episodearchived-替代-contextcompressed)
- [8. 集成测试 — fold 事件处理](#8-集成测试--fold-事件处理)
- [9. E2E 测试 — 长任务稳定性](#9-e2e-测试--长任务稳定性)
- [10. 契约测试 — 数据结构一致性](#10-契约测试--数据结构一致性)
- [11. 回归测试](#11-回归测试)
- [12. 验收标准覆盖矩阵](#12-验收标准覆盖矩阵)

---

## 1. 测试策略总览

### 1.1 分层结构

```
┌──────────────────────────────────────────────────────┐
│  E2E 测试 (tests/test_context_manager.py 扩展)       │
│  50 轮长任务不溢出 / 三层压缩逐级触发                   │
├──────────────────────────────────────────────────────┤
│  集成测试 (tests/test_context_manager.py 扩展         │
│           + tests/test_fold.py 扩展)                  │
│  ContextManager + EventStore + fold_events 交互        │
├──────────────────────────────────────────────────────┤
│  单元测试 (tests/test_token_counter.py 新增           │
│           + tests/test_events.py 扩展)                │
│  TokenCounter / Episode 序列化 / 重要性评分纯函数       │
├──────────────────────────────────────────────────────┤
│  契约测试 (Pydantic Schema vs EventStore 写入)         │
│  EpisodeArchived / ContextPruned 结构一致性             │
└──────────────────────────────────────────────────────┘
```

### 1.2 测试文件映射

```
tests/
├── test_token_counter.py          # 新增 — TokenCounter 三级实现
├── test_context_manager.py        # 扩展 — 三层压缩 / EpisodeArchived / ContextPruned
├── test_fold.py                   # 扩展 — EPISODE_ARCHIVED / CONTEXT_PRUNED fold
├── test_events.py                 # 扩展 — Episode / 新事件 Payload 序列化
└── conftest.py                    # 扩展 — TokenCounter fixtures
```

### 1.3 关键约束

- **EventStore = `:memory:` SQLite**（集成测试均用内存存储）
- **MockLLMClient** 用于模拟 LLM 输出（不调真实 API）
- **MockAgentKernel** 用于 E2E 场景
- **不依赖外部 DB / 网络 / 文件系统**

---

## 2. 测试环境与基础设施

### 2.1 fixtures 扩展（conftest.py 新增）

```python
# 新增到 conftest.py

@pytest.fixture
def mock_llm_for_episode():
    """Mock LLM that returns valid structured Episode JSON."""
    import json
    return MockLLMClient([json.dumps({
        "title": "User Authentication Module",
        "summary": "Implemented login, registration, and token refresh.",
        "key_decisions": ["Use JWT for auth", "bcrypt for password hashing"],
        "tools_used": ["file_write", "shell"],
        "key_findings": ["Found existing auth middleware"],
        "errors_encountered": ["Port conflict on first deploy"],
        "current_plan": "Add rate limiting",
    })])


@pytest.fixture
def mock_llm_legacy():
    """Mock LLM that returns non-JSON plain text (legacy format)."""
    return MockLLMClient(["Plain text summary: agent did X then Y then Z"])


@pytest.fixture
def mock_llm_malformed_json():
    """Mock LLM that returns invalid JSON."""
    return MockLLMClient(['{"key_decisions": ["incomplete'])


@pytest.fixture
def heuristics_counter():
    """Heuristic token counter instance."""
    from harness.core.token_counter import HeuristicTokenCounter
    return HeuristicTokenCounter()


@pytest.fixture
def tiktoken_counter():
    """Tiktoken counter instance. Skip on import error."""
    import pytest
    try:
        from harness.core.token_counter import TiktokenTokenCounter
        return TiktokenTokenCounter()
    except ImportError:
        pytest.skip("tiktoken not installed")
```

### 2.2 新增 markers

```python
# pyproject.toml 或 pytest.ini 追加
markers = [
    "token_counter: TokenCounter 三级实现测试",
    "episode_archive: EpisodeArchived + ContextPruned 事件测试",
    "importance_scoring: 重要性评分纯函数测试",
    "fold_v3: fold 新事件类型测试",
]
```

---

## 3. 单元测试 — TokenCounter

**文件**: `tests/test_token_counter.py`（新建，约 200 行）

### 3.1 TiktokenTokenCounter

> **前置**: `pip install tiktoken`。tiktoken 不可用时标记为 skip。

| # | 测试用例 | 输入 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| TC-U1 | `count("hello world")` 返回正整数 | `"hello world"` | `result > 0` | AC-1 |
| TC-U2 | `count("")` 返回 0 | `""` | `== 0` | AC-1 |
| TC-U3 | `count(None)` 返回 0（防御性） | `None` | `== 0` | AC-1 |
| TC-U4 | 纯英文计数 | `"A" * 1000` | 与 tiktoken 原生 encode 一致 | AC-1 |
| TC-U5 | 中英混合计数 | `"你好 world"` | `> 0`，与原生 encode 一致 | AC-1 |
| TC-U6 | `count_messages` 聚合多条 | `[{"content": "a"}, {"content": "b"}]` | 等于 `count("a") + count("b")` | AC-1 |

### 3.2 HeuristicTokenCounter

| # | 测试用例 | 输入 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| TC-U7 | `count("hello")` = max(1, floor(5 × 0.25)) | `"hello"` | `== 1` | AC-3 |
| TC-U8 | `count("")` 返回 1（不为 0） | `""` | `== 1` | AC-3 |
| TC-U9 | 100 字符 ≈ 25 token | `"A" * 100` | `== 25` | AC-3 |

### 3.3 工厂函数与降级链

| # | 测试用例 | 输入 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| TC-U10 | `create_token_counter(strategy="auto")` 无 provider → 创建 TiktokenCounter | `"auto"` | `isinstance(x, TokenCounter)` | AC-2 |
| TC-U11 | `create_token_counter(strategy="heuristic")` 强制 heuristic | `"heuristic"` | `isinstance(x, HeuristicTokenCounter)` | AC-2 |
| TC-U12 | `TOKEN_COUNTER_STRATEGY=provider` 但 key 缺失 → 降级到 tiktoken | env: url 有, key 无 | `isinstance(x, TiktokenTokenCounter)` + 日志告警 | AC-2 |
| TC-U13 | 三级降级链：provider 抛异常 → tiktoken → heuristic | 模拟 provider 异常 | `isinstance(x, HeuristicTokenCounter)`，每级降级记录日志 | AC-2 |
| TC-U14 | 降级后 `count(text)` 仍能返回有效值 | 任意 text | `> 0`，不抛异常 | AC-2 |

### 3.4 环境变量解析

| # | 测试用例 | 设置 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| TC-U15 | `TOKEN_COUNTER_API_URL` + `TOKEN_COUNTER_API_KEY` 都设置 → provider 优先 | url + key 均有效 | `isinstance(x, ProviderTokenCounter)` | AC-3 |
| TC-U16 | 仅 `TOKEN_COUNTER_MODEL` 设置 → 影响 tiktoken encoding 选择 | `model="o200k_base"` | 计数器使用指定 encoding | — |

### 3.5 与 LLM usage 校验（不阻塞主流程）

| # | 测试用例 | 输入 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| TC-U17 | `TokenCounter.count_messages()` 与 `usage.prompt_tokens` 对比 | LLM 返回 usage={prompt_tokens: 100} | 偏差记录日志，不抛异常，不阻塞 | AC-1 |

---

## 4. 单元测试 — 事件模型

**文件**: `tests/test_events.py`（扩展，约 130 行）

### 4.1 Episode 模型

| # | 测试用例 | 输入 | 期望 |
|---|---------|------|------|
| EM-U1 | `Episode` 字段完整可用 | 构造 Episode | 可访问 `episode_range`, `key_decisions`, `title`, `summary`, … |
| EM-U2 | `Episode.title` 必填且非空 | `title=""` | Pydantic ValidationError |
| EM-U3 | `Episode.importance_score` 默认 0.0 | 不传 importance_score | `== 0.0` |
| EM-U4 | `Episode.embedding` 默认 None | 不传 embedding | `== None` |
| EM-U5 | `Episode.parent_episode_id` 默认 None | 不传 parent_episode_id | `== None` |
| EM-U6 | `Episode.format` 默认 "structured" | 不传 format | `== "structured"` |
| EM-U7 | `Episode.format="legacy"` 允许 | `format="legacy"` | 创建成功 |
| EM-U8 | limit::0,1 `Episode.model_dump_json()` 包含所有字段 | 完整 Episode | JSON 含 title/summary/importance_score... |
| EM-U9 | `importances_score` 范围 `[0.0, 1.0]` — 超出抛错 | `importance_score=1.5` | Pydantic ValidationError |

### 4.2 新事件 Payload

| # | 测试用例 | 输入 | 期望 |
|---|---------|------|------|
| EM-U11 | `EpisodeArchivedPayload` 构造全字段 | 传所有必填字段 | 构造成功 |
| EM-U12 | `EpisodeArchivedPayload.model_dump()` 与 Schema 一致 | 构造后 dump | JSON 含 `episode`(嵌套), `keep_recent_count`, `archived_event_refs` |
| EM-U13 | `ContextPrunedPayload` 构造 & dump | 构造全字段 | JSON 含 `pruned_event_refs`, `pruned_token_count`, `pruned_seq_count`, `reason` |
| EM-U14 | `ContextPrunedPayload.reason` 默认 "lazy_clear" | 不传 reason | `== "lazy_clear"` |

### 4.3 EventType 枚举

| # | 测试用例 | 期望 |
|---|---------|------|
| EM-U15 | `EventType.EPISODE_ARCHIVED` 存在 | `"EpisodeArchived"` |
| EM-U16 | `EventType.CONTEXT_PRUNED` 存在 | `"ContextPruned"` |
| EM-U17 | `EventType.CONTEXT_COMPRESSED` 保留（不删除） | `"ContextCompressed"` |

### 4.4 PAYLOAD_MODEL_MAP 注册

| # | 测试用例 | 期望 |
|---|---------|------|
| EM-U18 | `PAYLOAD_MODEL_MAP[EventType.EPISODE_ARCHIVED]` → `EpisodeArchivedPayload` | 正确映射 |
| EM-U19 | `PAYLOAD_MODEL_MAP[EventType.CONTEXT_PRUNED]` → `ContextPrunedPayload` | 正确映射 |
| EM-U20 | `PAYLOAD_MODEL_MAP[EventType.CONTEXT_COMPRESSED]` 保留原有映射 | 正确映射到 `ContextCompressedPayload` |

---

## 5. 单元测试 — 重要性评分与剪枝

**文件**: `tests/test_context_manager.py`（扩展，约 120 行）

### 5.1 `_score_event_importance` 纯函数

| # | 测试用例 | 输入 | 期望分数 | 对应 PRD |
|---|---------|------|---------|---------|
| IS-U1 | 用户指令（UserInputReceived 语义） | `thought="user said: create a login page"` + 特殊标记 | `>= 0.9` | 高 (0.9) |
| IS-U2 | ToolFailed result | `ToolResult(status=FAILED, tool_name="x", error="...")` | `>= 0.8` | 高 (0.8) |
| IS-U3 | GuardrailTriggered result | `ToolResult(status=GUARDRAIL_BLOCKED, ...)` | `>= 0.8` | 高 (0.8) |
| IS-U4 | PlanCreated / PlanStepCompleted seq 附近 thought | `ThoughtEntry(seq=planed_seq+1)` | `>= 0.7` | 高 (0.7) |
| IS-U5 | 普通 Agent thought | `ThoughtEntry(thought="hmm let me think...")` | `0.4–0.6` | 中 (0.5) |
| IS-U6 | ToolSucceeded result（已处理） | `ToolResult(status=COMPLETED, output="done")` | `<= 0.3` | 低 (0.2) |
| IS-U7 | 重复工具调用（相同 tool_name + input hash） | 第 2+ 次相同 tool+input | `<= 0.2` | 低 (0.1) |

### 5.2 剪枝排序

| # | 测试用例 | 输入 | 期望 |
|---|---------|------|------|
| IS-U8 | 高重要性事件在 RunState 中保留 | 混合高/低重要性事件 | `thought_history` 中高危事件不因剪枝移除 |
| IS-U9 | 低重要性事件优先从 RunState 移除 | 混合高/低重要性事件 | `pruned_refs` 优先包含低分事件 |
| IS-U10 | `_select_low_importance_events` 返回候选引用的顺序 | 混合事件列表 | 返回按分数升序，低分在前 |

---

## 6. 集成测试 — 三层压缩策略

**文件**: `tests/test_context_manager.py`（扩展，约 200 行）

### 6.1 惰性清理（Lazy Clear, >50%）

| # | 测试用例 | 前置 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| CM-I1 | token > 50% 且 <=70% → 写入 `ContextPruned` 事件 | 填充 state 至 ~55% token 预算 | EventStore 有 `CONTEXT_PRUNED`，无 `CONTEXT_COMPRESSED` | AC-9 |
| CM-I2 | 惰性清理不写 `EpisodeArchived` | 同上 | EventStore 有 `CONTEXT_PRUNED`，无 `EPISODE_ARCHIVED` | AC-9 |
| CM-I3 | `ContextPruned` 包含 `pruned_token_count`, `pruned_seq_count`, `reason` | 同上 | payload 字段均非空 | AC-9 |
| CM-I4 | 惰性清理移除低重要性 ToolResult/ThoughtEntry | 填充多个低分工具输出 | `pruned_event_refs` 只引用低分事件 | AC-9 |
| CM-I5 | 惰性清理保留关键决策和错误 | 填充含错误的事件 | 关键决策和错误不在 `pruned_event_refs` 中 | AC-6 |

### 6.2 情节归档（Episode Archive, >70%）

| # | 测试用例 | 前置 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| CM-I6 | token > 70% 且 <=90% → 写入 `EpisodeArchived` 事件 | 填充 state 至 ~80% | EventStore 有 `EPISODE_ARCHIVED`，无 `CONTEXT_COMPRESSED` | AC-10 |
| CM-I7 | `EpisodeArchived.episode.title` 非空 | 使用 mock_llm_for_episode | `title != ""` | AC-5 |
| CM-I8 | `EpisodeArchived.episode.key_decisions` 至少 1 条 | 使用 mock_llm_for_episode | `len(key_decisions) >= 1` | AC-5 |
| CM-I9 | `EpisodeArchived.episode.format == "structured"` | LLM 返回合法 JSON | `format == "structured"` | AC-4 |
| CM-I10 | `EpisodeArchived.episode.format == "legacy"`（无 LLM 降级） | `ContextManager(llm_client=None)` | `format == "legacy"` | AC-4 |
| CM-I11 | `EpisodeArchived.episode.format == "legacy"`（LLM 返回非 JSON） | mock_llm_legacy | `format == "legacy"` | AC-4 |
| CM-I12 | `EpisodeArchived.episode.format == "legacy"`（LLM 返回损坏 JSON） | mock_llm_malformed_json | `format == "legacy"` | AC-4 |
| CM-I13 | `EpisodeArchived.keep_recent_count` 写入正确值 | 正常压缩 | `keep_recent_count >= 2` | — |
| CM-I14 | `EpisodeArchived.archived_event_refs` 非空 | 有事件被归档 | `len(archived_event_refs) > 0` | — |

### 6.3 紧急压缩（Emergency, >90%）

| # | 测试用例 | 前置 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| CM-I15 | token > 90% → 紧急模式 | 填充 state 至 ~95% | `EpisodeArchived.keep_recent_count == 3` | AC-7 |
| CM-I16 | 紧急模式保留最近 3 轮 | 10 轮历史 | `keep_recent_count == 3` | AC-7 |
| CM-I17 | 紧急模式压缩最旧 50% 事件 | 10 轮历史 | `archived_event_refs` 包含前 5 轮的 seq | AC-7 |

### 6.4 触发条件边界

| # | 测试用例 | 前置 | 期望 |
|---|---------|------|------|
| CM-I18 | token <= 50% → 不处理 | state 几乎为空 | 不写入任何压缩/剪枝事件 |
| CM-I19 | token 恰好 51% → 惰性清理 | 控制 token 到 51% | 写入 `CONTEXT_PRUNED` |
| CM-I20 | token 恰好 71% → 情节归档 | 控制 token 到 71% | 写入 `EPISODE_ARCHIVED` |
| CM-I21 | token 恰好 91% → 紧急压缩 | 控制 token 到 91% | `keep_recent_count == 3` |

### 6.5 冷却（cooldown）机制

| # | 测试用例 | 前置 | 期望 |
|---|---------|------|------|
| CM-I22 | 同 iteration 不重复压缩 | 连续两次 `maybe_compress` 同 iter | 只写入 1 条事件 |
| CM-I23 | cooldown 间隔内被跳过 | `checkpoint_interval=3`，1→2→3 连续触发 | 只有 iter=1,4,7 写入事件 |

### 6.6 参数化配置

| # | 测试用例 | 配置 | 期望 |
|---|---------|------|------|
| CM-I24 | `lazy_clear_ratio=0.4` 提前触发惰性清理 | `lazy_clear_ratio=0.4` | 40% token 时写入 `CONTEXT_PRUNED` |
| CM-I25 | `compression_threshold_ratio=0.6` 提前归档 | `compression_threshold_ratio=0.6` | 60% token 时写入 `EPISODE_ARCHIVED` |
| CM-I26 | `emergency_threshold_ratio=0.95` 延迟紧急 | `emergency_threshold_ratio=0.95` | 95% token 时才紧急模式 |

---

## 7. 集成测试 — EpisodeArchived 替代 ContextCompressed

**文件**: `tests/test_context_manager.py`（扩展，约 60 行）

| # | 测试用例 | 前置 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| RC-U1 | 新 Run 不产生 `ContextCompressed` | 正常执行到压缩触发 | EventStore 无 `CONTEXT_COMPRESSED` | AC-10 |
| RC-U2 | 新 Run 产生 `EpisodeArchived` | 同上 | EventStore 有 `EPISODE_ARCHIVED` | AC-10 |
| RC-U3 | `CONTEXT_COMPRESSED` 枚举值仍然存在 | — | `EventType.CONTEXT_COMPRESSED` 仍可访问 | AC-11 |
| RC-U4 | `ContextCompressedPayload` 仍可被 Pydantic 解析 | 构造老格式 payload | `.model_validate()` 不抛错 | AC-11 |

---

## 8. 集成测试 — fold 事件处理

**文件**: `tests/test_fold.py`（扩展，约 150 行）

### 8.1 `EPISODE_ARCHIVED` fold

| # | 测试用例 | 事件流 | 期望 | 对应 AC |
|---|---------|--------|------|---------|
| FL-I1 | `EPISODE_ARCHIVED` 设置 `state.summary = Episode` | `[RUN_STARTED, ..., EPISODE_ARCHIVED]` | `isinstance(state.summary, Episode)` | — |
| FL-I2 | `EPISODE_ARCHIVED` 追加到 `state.episodes` | 同上 | `len(state.episodes) == 1` | — |
| FL-I3 | 多次 `EPISODE_ARCHIVED` → `state.episodes` 累积 | `[..., EPISODE_ARCHIVED, ..., EPISODE_ARCHIVED]` | `len(state.episodes) == 2` | — |
| FL-I4 | `state.episodes` 保持插入顺序 | 两次归档 | `episodes[0]` 是更早的那次 | — |
| FL-I5 | `EPISODE_ARCHIVED` 设置 `state.keep_recent_count` | 事件含 `keep_recent_count=3` | `state.keep_recent_count == 3` | — |
| FL-I6 | fold 后 `thought_history` 中被归档的旧事件被移除 | 归档事件包含前 N 轮的 seq | `thought_history` 只剩 `keep_recent_count` 最近轮 | AC-6 |
| FL-I7 | fold 后 `tool_results` 中被归档的旧事件被移除 | 同上 | `tool_results` 只剩最近轮 | AC-6 |
| FL-I8 | 最近轮（keep_recent_count 内）事件不被移除 | 归档第 1-7 轮，keep=3 | 第 8-10 轮的 thought 仍保留 | AC-6 |

### 8.2 `CONTEXT_PRUNED` fold

| # | 测试用例 | 事件流 | 期望 | 对应 AC |
|---|---------|--------|------|---------|
| FL-I9 | `CONTEXT_PRUNED` 从 `thought_history` 移除指定事件 | `[RUN_STARTED, ..., CONTEXT_PRUNED]` | `pruned_refs` 中 seq 的 thought 不在 history 中 | AC-9 |
| FL-I10 | `CONTEXT_PRUNED` 从 `tool_results` 移除指定事件 | 同上 | `pruned_refs` 中 seq 的 tool_result 不在 results 中 | AC-9 |
| FL-I11 | `CONTEXT_PRUNED` 不影响 EventStore 原始事件 | 查询 EventStore | 原始事件仍存在 | AC-12 |

### 8.3 兼容性 — 老 `CONTEXT_COMPRESSED` 仍可 fold

| # | 测试用例 | 事件流 | 期望 |
|---|---------|--------|------|
| FL-I12 | 老格式 `CONTEXT_COMPRESSED(summary_ref=Episode)` fold 正常 | 构造老事件 | `state.summary` 被设置 |
| FL-I13 | 老格式 `CONTEXT_COMPRESSED(summary_ref=str)` fold 正常 | `summary_ref="summary text"` | `state.summary == "summary text"` |
| FL-I14 | `CONTEXT_COMPRESSED` 不追加到 `state.episodes`（legacy 路径） | 老事件 | `len(state.episodes) == 0` |
| FL-I15 | 混合新旧事件流 fold 正常 | `[CONTEXT_COMPRESSED, ..., EPISODE_ARCHIVED]` | 不抛异常，两者各司其职 |

---

## 9. E2E 测试 — 长任务稳定性

**文件**: `tests/test_context_manager.py`（扩展，约 80 行）

| # | 测试用例 | 前置 | 期望 | 对应 AC |
|---|---------|------|------|---------|
| E2E-1 | 50 轮长任务完整完成 | `token_limit=1000`, MockLLMClient + MockAgentKernel | `result.status == "completed"` | AC-7 |
| E2E-2 | 50 轮中至少触发 1 次惰性清理 | 同上 | EventStore 有 `CONTEXT_PRUNED` 事件 | AC-7 |
| E2E-3 | 50 轮中至少触发 1 次情节归档 | 同上 | EventStore 有 `EPISODE_ARCHIVED` 事件 | AC-7 |
| E2E-4 | 50 轮不因上下文溢出中断 | 同上 | 无 `RunFailed` 事件 | AC-7 |
| E2E-5 | 长任务中压缩后 Agent 仍能正确推理 | 归档后给一个依赖历史的 prompt | 后续 thought 引用前面 episode 的信息 | AC-7 |

> **注意**: E2E-5 依赖 `LLMAgentKernel._build_history_messages` 正确使用 `state.episodes`。当前 Phase 1 边界内只测 `state.summary` 路径，Phase 2 扩展。

---

## 10. 契约测试 — 数据结构一致性

| # | 测试用例 | 验证方式 | 对应 AC |
|---|---------|---------|---------|
| CT-1 | `Episode.model_json_schema()` 包含所有新增字段 | 对比 PRD Section 3.2 字段列表 | AC-4 |
| CT-2 | `EpisodeArchivedPayload.model_json_schema()` 与 PRD Section 3.5 一致 | 对比 PRD | — |
| CT-3 | `ContextPrunedPayload.model_json_schema()` 与 PRD Section 4.3 一致 | 对比 PRD | AC-9 |
| CT-4 | `EpisodeArchived` → EventStore → 读取 → 字段不丢失 | 写-读-反序列化 | — |
| CT-5 | `ContextPruned` → EventStore → 读取 → 字段不丢失 | 写-读-反序列化 | — |

---

## 11. 回归测试

| # | 测试范围 | 验证方式 | 通过标准 |
|---|---------|---------|---------|
| RT-1 | 现有 `tests/test_context_manager.py` 全部 20+ 用例 | `pytest tests/test_context_manager.py -v` | 100% 通过 |
| RT-2 | 现有 `tests/test_fold.py` 全部 40+ 用例 | `pytest tests/test_fold.py -v` | 100% 通过 |
| RT-3 | 现有 `tests/test_agent_kernel.py`（如有） | `pytest tests/test_agent_kernel.py -v` | 100% 通过 |
| RT-4 | 全量测试 | `pytest tests/` | 原有 719+ 通过，新增也全部通过 |

> **刚性要求**: 回归测试不得有任何退化。旧接口行为（`ContextManager.__init__` 默认参数、`fold_events` 旧事件处理）必须保持向后兼容。

---

## 12. 验收标准覆盖矩阵

| PRD AC | 描述 | 覆盖测试用例 |
|--------|------|-------------|
| AC-1 | tiktoken 计数误差 < 5% | TC-U1–TC-U6, TC-U17 |
| AC-2 | 三级策略自动降级 | TC-U10–TC-U14 |
| AC-3 | API key 写入 `.env` 后自动切换 | TC-U15 |
| AC-4 | Episode JSON 解析成功率 ≥ 95% | CM-I9–CM-I12 |
| AC-5 | title 非空，key_decisions ≥ 1 | CM-I7, CM-I8 |
| AC-6 | 关键决策/错误在剪枝后存在于摘要 | CM-I5, FL-I6, FL-I7, FL-I8 |
| AC-7 | 50 轮长任务不溢出 | CM-I15–CM-I17, E2E-1–E2E-5 |
| AC-8 | 现有 context_manager 测试不破坏 | RT-1 |
| AC-9 | 惰性清理写入 ContextPruned | CM-I1–CM-I5, FL-I9–FL-I11 |
| AC-10 | 新 Run 只写 EpisodeArchived，不写 ContextCompressed | CM-I6, RC-U1, RC-U2 |
| AC-11 | 老 ContextCompressed 仍可正确 fold | RC-U3, RC-U4, FL-I12–FL-I15 |
| AC-12 | EventStore 原始事件不因剪枝被删除 | FL-I11 |

---

## 附录 A: 测试用例统计

| 文件 | 类型 | 新增用例数 |
|------|------|-----------|
| `tests/test_token_counter.py` | 单元 | ~17 |
| `tests/test_events.py` | 单元 | ~22 |
| `tests/test_context_manager.py` | 集成 + E2E | ~36 |
| `tests/test_fold.py` | 集成 | ~15 |
| `tests/test_context_manager.py` (E2E) | E2E | ~5 |
| 契约测试 | 契约 | ~5 |
| **合计** | | **~100** |

---

## 附录 B: 已知边界场景

| 场景 | 处理方案 | 是否需额外测试 |
|------|---------|---------------|
| tiktoken 包未安装 | `create_token_counter("auto")` 降级到 `HeuristicTokenCounter` | TC-U14 已覆盖 |
| Provider API 超时 | 降级到下一级 | TC-U13 已覆盖 |
| LLM 返回空字符串 | 降级为 `format: "legacy"`，`current_plan=""` | 已有 CM-I10 覆盖无 LLM 场景 |
| `state.thought_history` / `tool_results` 为空 | 不触发压缩 | CM-I18 已覆盖 |
| 并行压缩（同一 run_id 并发 maybe_compress） | cooldown 机制防重 | CM-I22, CM-I23 已覆盖 |
| importances_score 边界 (0.0 / 1.0) | 允许 | EM-U9 已覆盖 |

---

*本文件基于 `PRD_v3.0_Phase1_上下文压缩与剪枝优化.md` 和 `ARCHITECTURE_v3.0_Phase1.md` 编写，测试用例 ID 对应架构文档 Section 10–12 的验收标准。*
