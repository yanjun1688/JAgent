# PRD v3.0 Phase 2: Agent 记忆系统 — 检索、持久化与自主学习

> **版本**: v3.0 Phase 2
> **状态**: Draft — 待 PM 审查
> **日期**: 2026-07-27
> **前置依赖**: PRD v3.0 Phase 1（结构化 Episode + Token 精准化 + 重要性剪枝）已完成
> **范围**: 在 Phase 1 的基础上，扩展为完整的 Agent 多层记忆系统
> **架构约束**: 
> - 所有状态来自 EventStore 事件流折叠，EventStore 是唯一 truth source
> - 新增存储层（MemoryStore 等）必须是 EventStore 的只读投影/索引，可重建
> - Scheduler / AgentKernel 恢复时只读 EventStore，不直接读 MemoryStore 或文件系统

---

## 1. 问题陈述

### 1.1 Phase 1 已经做了什么

| 能力 | Phase 1 状态 |
|------|-------------|
| Token 精准计数 | 有 |
| 压缩时生成结构化 Episode（标题、决策、错误、重要性） | 有 |
| 按重要性剪枝（关键信息保留、冗余从 RunState 移除） | 有 |
| 三层压缩策略（惰性清理 → 情节归档 → 紧急压缩） | 有 |
| EpisodeArchived 事件写入 EventStore | 有 |

### 1.2 Phase 1 之后 Agent 仍然做不到的事

| 场景 | 当前表现 | 根因 |
|------|---------|------|
| Agent 需要回忆"之前那个关于数据库配置的讨论" | Episode 存在 EventStore 中，但 fold 时只保留最新一条 state.summary | Episode 无检索手段，旧的被新摘要覆盖 |
| 任务暂停后恢复 | 新 session fold EventStore 只恢复最新的 Episode 摘要，前面的全丢 | fold_events 不累积历史 Episode |
| Agent 在长任务中犯第 3 次同样的错误 | 每次压缩独立，错误模式不跨 Episode 积累 | 无语义记忆提炼 |
| Agent 需要自己记笔记（"测试清单已完成 7/12"） | 无工具，只能靠上下文窗口记住 | 无 Agentic 记忆工具 |
| 跨多个 Run 的项目级知识（"这个项目部署在 K8s 上"） | 每个 Run 从头发现一遍 | 无跨 Run 持久化知识 |
| 前端展示 Agent 执行历史 | 只能看到原始事件流 | Episode 未暴露为可展示的结构 |

### 1.3 产品目标

> 让 Agent 拥有真正的"记忆"——不只是压缩后的一堆文本，而是**可检索、可提炼、可持久化**的多层记忆系统。

---

## 2. 用户场景

| # | 场景 | Phase 1 体验 | Phase 2 期望 |
|---|------|-------------|-------------|
| US-1 | Agent 执行 50 轮后需要找到"之前那个关于日志格式的讨论" | 在上下文窗口里翻，找不到就傻了 | Agent 调用 memory_search("日志格式")，精准召回相关 Episode |
| US-2 | 任务暂停→恢复（跨 session） | 只有最后一个 Episode 摘要 | fold_events 恢复所有历史 Episode 摘要 + 语义记忆 |
| US-3 | Agent 反复遇到同样的包安装失败 | 每次重新摸索，浪费 token | 语义记忆中记录了修复方案，自动注入上下文 |
| US-4 | Agent 需要追踪复杂任务的进度 | 记在"脑子"里，丢了就忘了 | Agent 用 memory_write 写笔记，通过 AgenticNoteStored 事件持久化 |
| US-5 | 多个 Run 在同一个代码仓库上工作 | 每个 Run 都重新探索项目结构 | 语义记忆持久化项目知识（目录结构、技术栈、约定） |
| US-6 | 前端展示 Agent 执行历史 | 只能看到事件流 | 按 Episode 分段浏览 + 关键决策时间线 |

---

## 3. 功能需求

### 3.1 Episode 累积与跨 Session 恢复 (P0)

**需求**: RunState 中的 Episode 摘要不互相覆盖，而是累积。跨 session 恢复时通过 EventStore 恢复完整的 Episode 历史。

**核心设计 — EventStore 是唯一 truth source**:

```
EventStore (Append-Only)
    │
    ├── EpisodeArchived #1 (seq 5-18)
    ├── EpisodeArchived #2 (seq 19-35)
    ├── EpisodeArchived #3 (seq 36-52)
    │   ...
    │
    ├──→ fold_events() 时累积所有 Episode 到 RunState.episodes[]
    │
    └──→ MemoryStore 订阅 EpisodeArchived 事件，构建检索索引（只读投影）
```

**RunState 变更**:

```python
# Phase 1（当前）:
state.summary: Episode | str | None        # 只保留最新一条

# Phase 2:
state.episodes: list[Episode]              # 累积所有 Episode
```

**跨 session 恢复流程**:

1. 新 session 启动 → `fold_events(events)` 遍历该 Run 的所有事件
2. 遇到 `EpisodeArchived` → `state.episodes.append(episode)`
3. state.summary 保持不变（最新一条，供 AgentKernel 向后兼容）
4. AgentKernel.think() 时从 `state.episodes` 注入最近的 Episode 摘要

**MemoryStore 的角色**（只读投影，可重建）:
- 订阅 `EventStore.append_event`，当事件类型为 `EpisodeArchived` 时更新索引
- 仅供 `RetrievalEngine` 加速语义检索
- 系统启动时从 EventStore 全量重建
- **不作为恢复数据源** — Scheduler 恢复时只读 EventStore

**验收**:
- 暂停→恢复后 state.episodes 包含该 Run 全部历史 Episode
- 恢复后的 Agent 能回答"之前做了什么"（基于累积的 Episode 摘要）
- MemoryStore 清空后可从 EventStore 完整重建
- 恢复后 token 消耗不因缺失上下文增加 > 20%

---

### 3.2 记忆检索 — RetrievalEngine (P0)

**需求**: Agent 能按语义搜索历史 Episode 和语义记忆。

**3.2.1 检索触发方式**

| 触发方式 | 时机 | 谁控制 |
|---------|------|--------|
| **自动注入** | 每次 THINK 前，Scheduler 检查是否需要注入相关记忆 | 系统（受信组件） |
| **Agent 主动查询** | Agent 调用 memory_search 工具 | Agent（非受信组件，走 Tool Layer） |
| **错误触发** | ToolFailed 事件发生后 | 系统（受信组件） |

**3.2.2 检索范围与数据源**

| 记忆层 | 检索内容 | 数据源 |
|--------|---------|--------|
| 情节记忆 | Episode.title + Episode.summary + Episode.key_decisions | MemoryStore（EventStore 投影） |
| 语义记忆 | SemanticMemory.content | SemanticMemoryStore（EventStore 投影） |

**3.2.3 检索算法**

混合排序 = **语义相似度 × α + 时效性衰减 × β + 重要性 × γ**

- 语义相似度：query 嵌入向量 vs 记忆条目的余弦相似度
- 时效性衰减：`e^(-λ × age)`，age 为距今的轮数
- 重要性加权：`importance_score`（Phase 1 已有）
- 默认权重：α=0.5, β=0.3, γ=0.2（可配置）

**3.2.4 向量索引方案**

Phase 2 采用**内存向量索引 MVP**：
- 使用 numpy / sklearn 在内存中维护嵌入矩阵
- MemoryStore 在内存中持有 `{memory_id: embedding}` 的 numpy array
- 检索时用 `cosine_similarity(query_embedding, matrix)` 排序
- 不引入外部向量数据库（Chroma / Milvus）
- 数据量预估：Phase 2 范围内 100-500 条记忆 × 1536 维 = 1-8MB 内存，numpy 完全可支撑

**3.2.5 检索结果注入格式**

```xml
<retrieved_memories>
  <episodes>
    <episode range="seq_12-seq_45" relevance="0.82" title="用户认证模块">
      <summary>实现了 JWT + bcrypt 方案...</summary>
      <key_decisions>
        <decision>选择 PostgreSQL 替代 SQLite 以支持并发</decision>
      </key_decisions>
    </episode>
  </episodes>
  <semantic_memories>
    <memory type="error_pattern" relevance="0.76">
      pip install 失败通常是虚拟环境未激活，修复: source .venv/bin/activate
    </memory>
    <memory type="decision" relevance="0.70">
      数据库使用 PostgreSQL 14，连接串存储在 .env 中
    </memory>
  </semantic_memories>
</retrieved_memories>
```

**验收**:
- Recall@5 ≥ 0.85（测试集：50 条记忆，10 个自然语言查询）
- 检索延迟 < 100ms (P95)
- 检索结果注入不超过 1000 token
- MemoryStore 从 EventStore 重建时间 < 500ms

---

### 3.3 语义记忆提炼 — SemanticMemoryStore (P1)

**需求**: 从多个 Episode 中自动提炼跨阶段、跨 Run 的持久化知识。

**核心设计 — EventStore 投影**:

```
SemanticMemoryStored 事件写入 EventStore
    │
    ▼
SemanticMemoryStore 订阅该事件，在内存/SQLite 中构建检索索引
```

真理源 = EventStore 中的 `SemanticMemoryStored` 事件。SemanticMemoryStore 是可重建的索引。

**3.3.1 语义记忆类型**

| 类型 | 内容 | 来源 | 生命周期 |
|------|------|------|---------|
| `decision` | 关键技术决策及原因 | Episode.key_decisions | 被新决策推翻时标记 superseded |
| `finding` | 已确认的事实/发现 | Episode.key_findings | 被新发现更新时覆盖 |
| `error_pattern` | 遇到的错误及修复方案 | Episode.errors_encountered | 永久保留（去重） |
| `project_context` | 项目级上下文（目录结构、技术栈） | Episode.key_findings + 工具输出 | 永久保留（更新覆盖） |
| `user_preference` | 用户偏好（语言、风格、环境） | UserInputReceived 事件 + Agent 推断 | 用户明确变更时覆盖 |

**3.3.2 提炼时机**

| 触发条件 | 行为 |
|---------|------|
| 每次 EpisodeArchived 事件写入后 | 异步提炼新的语义记忆条目 → 写入 SemanticMemoryStored 事件 |
| Run 结束时 | 批量提炼，去重合并已有语义记忆 |
| 手动触发 | 用户通过 API 触发 memory_reflect |

**3.3.3 去重与冲突解决**

- 同类型 + 高相似度 (cosine ≥ 0.85) → 合并为一条，保留最新版本
- 合并时设置 `supersedes_memory_id` 指向被合并的旧条目
- 新决策与旧决策冲突 → 旧决策标记为 `superseded`，保留但检索时降权，用 `supersedes_memory_id` 建立链式关系
- error_pattern 永久保留，按出现频率加权

**验收**:
- 跨 3 个 Episode 后语义记忆至少有 1 条提炼结果
- 相同错误模式出现 3 次后，第 4 次自动检索到修复方案
- 去重后语义记忆条目数增长曲线趋缓（不线性膨胀）
- SemanticMemoryStore 清空后可从 EventStore 中的 SemanticMemoryStored 事件完整重建

---

### 3.4 Agent 自主记忆工具 (P2)

**需求**: Agent 可以像人类工程师一样写笔记、列 TODO、记录进度。所有写入通过 Tool Layer 产生 EventStore 事件。

**核心设计 — 走 Tool Layer → Event Store**:

```
Agent 调用 memory_write 工具
    │
    ▼
Tool Layer: Guardrails 校验 (大小/速率/沙盒)
    │
    ├──→ 写入 AgenticNoteStored 事件到 EventStore
    │
    └──→ Tool Layer 内部写文件系统（沙盒工作区）
         注：文件系统写入是 Tool Layer 的实现细节，
         Scheduler 不直接读文件

恢复时:
    Scheduler fold_events → 读取 AgenticNoteStored 事件
    → 恢复出 state.agentic_notes
    → 注入 system prompt
```

**3.4.1 工具列表**

| 工具 | 描述 | 关键参数 |
|------|------|---------|
| `memory_write` | 写入一条结构化笔记 | `title`, `content` (Markdown), `tags[]`, `importance` (1-5) |
| `memory_read` | 按 memory_id 读取笔记全文 | `memory_id` |
| `memory_search` | 语义搜索 Agent 笔记 + 系统记忆 | `query`, `limit`, `scope` (agentic_only / all) |
| `memory_update` | 更新已有笔记 | `memory_id`, `content` |
| `memory_list` | 列出笔记标题和标签 | `tag`, `sort_by` (date / importance) |

**3.4.2 受信边界与系统强制约束**

| 约束 | 值 | 执行位置 | 理由 |
|------|-----|---------|------|
| 单条笔记大小上限 | 16 KB | Tool Layer Guardrails | 防止 Agent 塞入大量冗余数据 |
| 写入速率限制 | 10 次/分钟 | Tool Layer Guardrails | 防止 Agent 滥用 |
| 存储位置 | 沙盒工作区（`sessions/{run_id}/notes/`） | Tool Layer | 不跨项目泄露 |
| 写入幂等 | 由 Tool Layer 计算幂等键 (run_id + memory_id + content_hash) | Tool Layer | 防重 |
| 跨 session 加载 | fold_events 时恢复该 Run 所有 AgenticNoteStored 事件 | Scheduler | 恢复不依赖文件系统 |

**3.4.3 笔记格式建议（System Prompt 指令）**

Agent 的 system prompt 中包含记忆管理指导：
- 完成关键步骤后更新笔记
- 遇到错误时在笔记中记录根因和修复方案
- Session 结束前写简洁的 handoff 笔记
- 不要用笔记存储数据（工具输出），笔记是**提炼后的结构**

**验收**:
- Agent 在 5 个连续 session 的任务中能通过笔记保持连贯性
- 写入幂等：相同 content_hash 重复写入不产生重复 AgenticNoteStored 事件
- 笔记在沙盒隔离：Run A 的笔记对 Run B 不可见
- 恢复时不读文件系统，仅从 EventStore 折叠恢复

---

### 3.5 错误模式自动学习 (P2)

**需求**: 当 Agent 遇到错误且成功修复后，系统自动提取为 error_pattern 语义记忆。

**修复判定标准**（系统自动检测，不依赖 Agent 标记）:

```
ToolFailed(tool=X, error=err)
    └──→ 连续同 tool_name 失败
         └──→ 随后同 tool_name ToolSucceeded
              └──→ 判定：已修复
              提取 (error_type, tool_name, fix_summary) 
              → 写入 SemanticMemoryStored 事件
```

fix_summary 简化记录：使用 ToolSucceeded 的输出摘要（取前 200 字符），不为空时直接使用，为空则用上一次 ToolFailed 的 error 字段作为触发器条件。

**注入时机**: 后续 Agent 遇到相似错误时，自动检索语义记忆，在 system prompt 中注入：

```
⚠️ 注意：你之前遇到过类似错误，当时的修复方案是：
"pip install 失败 → source .venv/bin/activate 激活虚拟环境"
```

**验收**: 同类错误第 3 次出现时 Agent 修复轮数 ≤ 2（无记忆时为 3-5 轮）。

---

## 4. 事件类型对齐

### 4.1 新增事件类型

| 事件类型 | 所属层级 | Payload | 触发者 |
|---------|---------|---------|--------|
| `EPISODE_ARCHIVED` | L3 Scheduler | EpisodeArchivedPayload | ContextManager（受信） |
| `SEMANTIC_MEMORY_STORED` | L3 Scheduler | SemanticMemoryStoredPayload | MemoryReflection（受信） |
| `AGENTIC_NOTE_STORED` | L2 Tool Layer | AgenticNoteStoredPayload | memory_write 工具 |
| `CONTEXT_PRUNED` | L3 Scheduler | ContextPrunedPayload | ContextManager（受信） |

> 注：记忆检索（RetrievalEngine）是 L3 Scheduler 内的**查询操作**，不产生事件。检索结果通过 system prompt XML 注入，不走 EventStore。

### 4.2 与现有事件类型的关系

- 新事件类型追加到 `EventType` 枚举末尾，不修改现有序号
- `EpisodeArchived` 完全替代 `ContextCompressed`（后者仅保留枚举值用于历史兼容）
- `AgenticNoteStored` / `ContextPruned` 符合 Tool Layer / Scheduler 事件命名规范
- 记忆检索不再产生事件（查询操作，结果注入 system prompt）

---

## 5. 非功能需求

### 5.1 性能

| 指标 | 要求 |
|------|------|
| 语义检索延迟 (P95) | < 100ms |
| EpisodeArchived → MemoryStore 索引更新 | < 10ms |
| 语义记忆提炼延迟（异步） | < 5s |
| 跨 session 恢复总耗时 | < 300ms（fold_events 全程） |
| MemoryStore 从 EventStore 全量重建 | < 500ms |

### 5.2 存储

| 存储目标 | 内容 | 预估容量 |
|---------|------|---------|
| EventStore（已有） | 所有事件，含 EpisodeArchived、SemanticMemoryStored、AgenticNoteStored | 增量 < 5KB/Run |
| MemoryStore（新增，内存投影） | Episode + 语义记忆的嵌入向量索引 | < 8MB 内存 |
| Agentic 文件（Tool Layer 内部） | Agent 笔记 | 50 篇 × 16KB = 800KB/沙盒 |

### 5.3 兼容性

- Phase 1 的所有功能保持不变
- 所有新事件符合现有一致性规则
- Agent 不感知记忆系统底层实现
- 现有 fold_events 逻辑可渐进扩展（新增 case 分支）

---

## 6. 边界声明（本 Phase 不做）

- 不做多 Agent 共享记忆
- 不做记忆版本管理（git-like history）
- 不做用户手动编辑/标注语义记忆的 UI
- 不做记忆导出/导入
- 不做基于情感的检索（只做语义 + 关键词）
- 不做自动记忆衰减（仅用时效性系数加权，不主动删除条目）
- 不做外部向量数据库（Chroma / Milvus 留到后续 Phase 评估）
- MemoryStore 不做持久化（每次启动从 EventStore 重建）

---

## 7. 验收标准总览

| # | 标准 | 所属功能 |
|---|------|---------|
| AC-1 | 暂停→恢复后 state.episodes 包含该 Run 全部历史 Episode | 3.1 |
| AC-2 | 恢复后 token 消耗不因缺失上下文增加 > 20% | 3.1 |
| AC-3 | MemoryStore 清空后可从 EventStore 完整重建 | 3.1 / 3.3 |
| AC-4 | Recall@5 ≥ 0.85（50 条记忆 × 10 查询） | 3.2 |
| AC-5 | 检索注入 ≤ 1000 token | 3.2 |
| AC-6 | 跨 3 个 Episode 后至少提炼 1 条语义记忆 | 3.3 |
| AC-7 | 相同错误第 3 次出现时 Agent 修复轮数 ≤ 2 | 3.3 / 3.5 |
| AC-8 | 5 个连续 session 中 Agent 通过笔记保持连贯性 | 3.4 |
| AC-9 | 笔记写入幂等，沙盒隔离有效 | 3.4 |
| AC-10 | 恢复时不读文件系统，仅从 EventStore 折叠恢复笔记 | 3.4 |
| AC-11 | 语义记忆去重有效（条目不线性膨胀） | 3.3 |
| AC-12 | Phase 1 功能不受影响（回归） | 全局 |

---

## 8. 与 Phase 1 的关系

```
Phase 1 (PRD v3.0 Phase 1)               Phase 2 (本文档)
───────────────────────────              ─────────────────────
Token 精准计数          ─────────────→ 更精确的检索阈值控制
结构化 Episode          ─────────────→ Episode 持久化 + 累积 + 检索
重要性评分              ─────────────→ 语义记忆提炼的基础信号
三层压缩策略            ─────────────→ 惰性清理 → 减少检索噪音
EpisodeArchived 事件    ─────────────→ MemoryStore 索引源
ContextCompressed(legacy) ───────────→ 保持兼容，不删除

新增 (Phase 2):
  无                     ─────────────→ RunState.episodes[] (累积)
  无                     ─────────────→ MemoryStore (EventStore 投影)
  无                     ─────────────→ SemanticMemoryStored 事件
  无                     ─────────────→ SemanticMemoryStore (EventStore 投影)
  无                     ─────────────→ AgenticNoteStored 事件
  无                     ─────────────→ memory_* 工具集 (Tool Layer)
  无                     ─────────────→ RetrievalEngine (L3 Scheduler 查询操作，不产生事件)
  无                     ─────────────→ 错误模式自动学习 (→ SemanticMemoryStored)
  无                     ─────────────→ supersedes_memory_id 合并链
```

---

*技术架构设计（事件 Schema、模块接口、工具契约）由 ARCHITECTURE_v3.0.md 定义。*
