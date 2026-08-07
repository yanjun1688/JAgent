# PRD v3.0 Phase 1: 上下文压缩与剪枝优化

> **版本**: v3.0 Phase 1
> **状态**: Draft — 待 PM 审查
> **日期**: 2026-07-27
> **范围**: 仅工作记忆层（上下文窗口）内的压缩与剪枝，不涉及语义记忆、向量检索、Agent 工具
> **架构约束**: 所有状态来自 EventStore 事件流折叠（Append-Only）。不新增独立数据源。

---

## 1. 问题陈述

### 1.1 当前行为

JAgent 的 `context_manager.py` 在 token 使用达到 80% 阈值时触发压缩：

```
历史事件 (Event Store 中全部保留)
    │
    │  fold_events → RunState (thought_history / tool_results)
    │  token > 80%
    ▼
LLM 将全部历史总结为一坨文本（旧纯文本摘要）
    │
    ├──→ 写入 ContextCompressed 事件 (EventType.CONTEXT_COMPRESSED)
    │
    └──→ 下一次 fold_events 时：
         读取 ContextCompressed → 设置 state.summary
         按 original_event_refs 从 RunState 中移除已压缩的 thought/tool_result
         保留最近 keep_recent_count 轮
          
注意：剪枝只移除 RunState 中的内存对象，Event Store 中的原始事件完好无损。
```

### 1.2 用户能感知的三个问题

| 问题 | 表现 | 根因 |
|------|------|------|
| **压缩后 Agent 变傻** | 摘要是一坨长文本，LLM 注意力被稀释，关键信息找不到 | 摘要无结构，所有信息混在一起 |
| **重要信息被丢弃** | 剪枝按 seq 二分，中间阶段的关键决策和后面阶段的冗余输出被同等对待 | 无重要性区分，粗暴 FIFO |
| **Token 预算不可靠** | char × 0.25 估算中文误差可达 40%，导致压缩过早或过晚 | 无精确 token 计数 |

### 1.3 产品目标

> 让压缩后的上下文对 Agent **真正有用**，而不是糊弄系统。

---

## 2. 用户场景

| 场景 | 当前体验 | Phase 1 后体验 |
|------|---------|---------------|
| Agent 执行 50 轮后压缩，需要知道"前面做了什么" | 一篇 500 词的流水账，重点淹没 | 分段结构化摘要，一眼看到 Episodes 的标题、关键决策、错误 |
| Agent 在压缩后发现之前的修复方案被剪枝了 | 摘要里没有，内存状态中也被移除 | 错误记录在 Episode.key_findings/errors_encountered 中保留 |
| 用户查看压缩后的上下文质量 | 无可见性 | 每个 Episode 有 title + key_decisions + errors，可读可追溯 |
| Token 计数偏差导致提前/延迟压缩 | 不知道还剩多少空间 | 精确 token 计数，压缩触发时机准确 |

---

## 3. 功能需求

### 3.1 Token 精准计数 (P0)

**需求**: 用精确计数替换 char × 0.25 启发式估算。计数策略可插拔，按优先级降级。

**计数策略优先级**（系统自动检测可用性）:

| 优先级 | 策略 | 依赖 | 精度 |
|--------|------|------|------|
| 1 | Provider tokenize API | `.env` 中的 `LLM_API_KEY` + provider endpoint | 100%（精确） |
| 2 | tiktoken 库本地计数 | `pip install tiktoken`，无网络依赖 | ~99%（本地模型等效） |
| 3 | char × 0.25 启发式 | 无 | ~70%（仅兜底） |

**降级行为**:
- 系统启动时检测 API 可用性。API 可用 → 使用优先级 1
- API 不可用（无 key / 超时 / 余额不足）→ 自动降级到优先级 2，日志告警
- tiktoken 不可用 → 降级到优先级 3（当前行为），日志告警

**配置方式**:
- 优先级 1 的 API endpoint 和 key 从 `.env` 读取，无需改代码
- 用户拿到正式 API key 后，更新 `.env` 即可自动切换到优先级 1
- 不强制要求 API key，系统在无 key 时正常降级运行

**验收**: 
- 100 条中文/英文/混合样本，tiktoken（优先级 2）误差 < 5%
- 三个优先级之间的切换自动化，无需修改代码
- 降级时有日志告警，告知当前使用的计数策略

---

### 3.2 Episode 模型 — 统一结构化摘要类型 (P0)

**需求**: 用单一 `Episode` 类型承载结构化摘要，删除独立的 `EpisodeSummary` 类型以减少模型冗余。

**Episode 字段**:
```python
class Episode(BaseModel):
    title: str                       # 一句话标题（"用户认证模块实现"）
    summary: str                     # 3-5 句叙事摘要
    importance_score: float = 0.0    # 重要性 0-1
    embedding: list[float] | None = None   # 语义嵌入向量，Phase 1 为 None（Phase 2 启用）
    parent_episode_id: str | None = None   # 合并/更新时指向前一个版本
    format: str = "structured"       # "structured" | "legacy"
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

**变更点**:
- `Episode` 直接继承 `BaseModel`，不再继承 `EpisodeSummary`
- 删除 `EpisodeSummary` 类型及所有导出
- 老 `ContextCompressed` 事件的 `summary_ref` 类型实际为 `Episode | str`；fold 时通过 `isinstance` 区分
- 新 `EpisodeArchived` 事件的 `episode: Episode` 替代 `summary_ref`
- 无 LLM 或解析失败时降级为 `format: "legacy"`

**验收**:
- Episode JSON 解析成功率 ≥ 95%
- `title` 必填且非空
- `key_decisions` 至少 1 条

---

### 3.3 重要性评分与剪枝 (P0)

**需求**: 剪枝不再是简单的 seq 二分法，而是按**信息重要性**决定从 RunState 中保留/移除。

**核心原则**: 剪枝不删除 Event Store 中的事件，仅影响 `fold_events` 时 RunState 的内存状态（thought_history / tool_results）。原始事件始终在 Event Store 中用于审计和重放。

**评分规则**（系统强制，不依赖 LLM）:

| 信息类型 | 默认重要性 | 剪枝行为 |
|---------|-----------|---------|
| 用户指令 (RunStarted.intent) | 高 (0.9) | 在 RunState 中优先保留 |
| 错误事件 (ToolFailed / GuardrailTriggered) | 高 (0.8) | 在 RunState 中优先保留 |
| 关键决策 (PlanCreated / PlanRevised / DagStepCompleted / 含决策语义的 thought) | 高 (0.7) | 保留到 Episode 摘要中 |
| Agent thinking | 中 (0.5) | 压缩到 Episode，从 RunState 移除 |
| 工具成功输出 (ToolSucceeded, 已被 Agent 处理) | 低 (0.2) | 优先从 RunState 移除 |
| 重复工具调用 (相同 tool_name + 参数 × N 次) | 低 (0.1) | 从 RunState 移除 |

**剪枝决策矩阵**:

```
            重要性高          重要性低
            ─────────         ─────────
最近事件    RunState 保留      RunState 保留
中间事件    RunState 保留      从 RunState 移除（已在 Episode 摘要中归档）
早期事件    从 RunState 移除    从 RunState 移除
           （已在 Episode 中）  （不归档，直接丢弃）
```

**验收**: 关键决策和错误在剪枝后仍存在于最新 Episode 摘要中。冗余工具输出被优先从 RunState 中移除。

---

### 3.4 三层压缩策略 (P1)

**需求**: 不再是 "80% 触发 → 全量压缩" 单一策略，按 token 占用分级响应。统一由 `ContextManager.maybe_compress()` 内部按状态决定，对 Scheduler 只暴露一个入口。

| 级别 | 触发条件 | 行为 |
|------|---------|------|
| **惰性清理** | > 50% token 预算 | 从 RunState 中移除已被 Agent 处理过的低重要性工具输出，写入 ContextPruned 事件记录清理量 |
| **情节归档** | > 70% token 预算 | 生成结构化 Episode 并写入 EpisodeArchived 事件，fold 时从 RunState 中按重要性移除事件 |
| **紧急压缩** | > 90% token 预算 | 激进压缩 + 保留最近 3 轮，忽略阶段边界和重要性评分 |

**执行者**: 统一由 `ContextManager` 内部判断，调用方（Scheduler）只需调 `maybe_compress()`。

**验收**: 50 轮长任务执行过程中至少触发 1 次惰性清理和 1 次情节归档，不会因上下文溢出中断。

---

### 3.5 EpisodeArchived 事件 — 完全替代 ContextCompressed (P0)

**需求**: `EpisodeArchived` 完全替代 `ContextCompressed`。`ContextCompressed` 废弃，不再写入新事件（fold.py 继续支持读取已有的 legacy 事件）。

**Payload 设计**:
```
EpisodeArchivedPayload {
    original_tokens: int
    compressed_tokens: int
    episode: Episode                   ← 结构化 Episode
    keep_recent_count: int
    archived_event_refs: [int]         ← 被归档的原始事件 seq 列表
}
```

**废弃策略**:
- 新代码只写 `EpisodeArchived`，不写 `ContextCompressed`
- `EventType.CONTEXT_COMPRESSED` 保留在枚举中但不新增写入路径
- fold.py 处理 `CONTEXT_COMPRESSED` 的 legacy 逻辑保留（读取历史 Run 时需要）
- 新 Run 的 fold.py 只识别 `EPISODE_ARCHIVED`

**验收**: 新 Run 不产生 ContextCompressed 事件。历史 Run 的 ContextCompressed 事件仍可正确 fold。

---

## 4. 非功能需求

### 4.1 兼容性

- `EpisodeSummary` 删除，`Episode` 作为唯一结构化摘要类型；旧引用需改为 `Episode`
- `AgentPhase.SUMMARIZE` prompt 扩展升级
- 老 `ContextCompressed` 事件仍可读，新 Run 只写 `EpisodeArchived` / `ContextPruned`
- Agent 行为不变——Agent 仍不感知压缩的存在
- Event Store 保持 Append-Only，不删除任何事件

### 4.2 性能

- Token 计数延迟 < 50ms（调用 API）
- Episode 生成延迟 < 2s（LLM 调用）
- 压缩不阻塞 Agent 当前轮执行

### 4.3 可观测性

- 每个 Episode 生成后日志可查（title、original_tokens、compressed_tokens、importance_score）
- 惰性清理写入 ContextPruned 事件（记录 pruned_token_count、pruned_seq_count）
- EpisodeArchived 事件写入 EventStore，可审计

---

## 5. 边界声明（本 Phase 不做）

- 不做语义记忆跨 Episode 提炼
- 不做向量嵌入生成和语义检索（embedding 字段预留但为 None）
- 不做 Agent 自主记忆工具
- 不做跨 Run 持久化记忆
- 不做前端 Episode 浏览 UI

---

## 6. 验收标准总览

| # | 标准 | 验证方式 |
|---|------|---------|
| AC-1 | tiktoken（优先级 2）计数误差 < 5% | 100 条中英混合样本对比 |
| AC-2 | 三级策略自动降级（API 不可用 → tiktoken → char 启发式） | 逐一模拟不可用场景 |
| AC-3 | API key 写入 `.env` 后自动切换为优先级 1 | 手动验证 |
| AC-4 | Episode JSON 解析成功率 ≥ 95% | 自动化 Schema 校验 |
| AC-5 | title 非空，key_decisions 至少 1 条 | LLM 输出验证 |
| AC-6 | 关键决策/错误在剪枝后存在于最新 Episode 摘要 | 集成测试 |
| AC-7 | 50 轮长任务不因上下文溢出中断 | 端到端测试 |
| AC-8 | 现有 context_manager 测试不破坏 | 回归测试 |
| AC-9 | 惰性清理写入 ContextPruned 事件 | 查询 EventStore |
| AC-10 | 新 Run 只写 EpisodeArchived，不写 ContextCompressed | 查询 EventStore |
| AC-11 | 历史 Run 的 ContextCompressed 事件仍可正确 fold | 回归测试 |
| AC-12 | Event Store 中原始事件不因剪枝而被删除 | 直接查 DB 验证 |
