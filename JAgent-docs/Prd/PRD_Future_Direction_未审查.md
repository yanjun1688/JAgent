# PRD：Harness 未来产品方向与用户需求（未审查，仅供参考）

> **状态**: 未作为正式审查文档 — 本文档记录当前对话中探讨的产品愿景与后续方向，**不代表当前阶段（v3.0 Phase 1）的交付承诺**。进入下一阶段前需重新评估、拆分并单独评审。

---

## 1. 用户需求分析

### 1.1 用户是谁

目标用户为**开发者 / 技术团队负责人**，需要构建或运行长周期、多步骤、可信任的 Agent 任务，而非单次对话式问答。

### 1.2 核心痛点

| 痛点 | 现有工具（ChatGPT / Claude Code / Cursor）的局限 | 用户期望的 Harness |
|------|----------------------------------------------|-------------------|
| 不可审计 | 对话历史是文本列表，难以追踪"Agent 为何做了某个决策" | 每个决策、每次工具调用、每次压缩都作为事件持久化 |
| 不可恢复 | 进程崩溃或会话结束即丢失上下文 | 从 Event Store 任意事件点恢复 Run |
| 不可调试 | 无法看 Agent 内部状态流转 | 事件流可视化时间线 + 折叠状态 diff |
| 单 Agent 瓶颈 | 一个 Agent 做所有事，能力边界和上下文都受限 | 多 Agent 分发：planner、executor、coder 各自走不同链路 |
| 权限模糊 | 工具执行者是谁、谁授权不明确 | 每个事件携带 Principal（user 或 agent），权限失败直接拦截 |
| 上下文爆炸 | 长任务历史塞满上下文窗口 | 结构化 Episode + 重要性剪枝 + 语义记忆检索 |

### 1.3 用户约束

- **不做 SaaS**：单用户 + 粗粒度角色即可，无需多租户
- **权限策略简单**：`admin / operator / viewer / agent_runner` 四种角色，权限失败直接抛 `GuardrailTriggered` 事件
- **Agent 身份独立**：Agent 不是用户，有自己的 `agent_id` 和 `agent_type`，未来多 Agent 交互时各自走独立链路
- **正确性优先于简单性**：愿意承担事件流架构的复杂性，以换取可审计、可恢复、可协作

---

## 2. 未来开发期望（按主题分组）

### 2.1 当前进行中：v3.0 Phase 1 — 上下文压缩与剪枝

- 用结构化 `Episode` 替代纯文本摘要
- 用 `TokenCounter` 精准计数替代 `char × 0.25`
- 用重要性评分替代 seq 二分剪枝
- 三级压缩策略：`lazy_clear` → `archive_episode` → `emergency_compact`
- 废弃 `CONTEXT_COMPRESSED` 写入，新 Run 只写 `EPISODE_ARCHIVED` / `CONTEXT_PRUNED`

### 2.2 近期：Agent 记忆系统（v3.0 Phase 2）

- Episode 跨会话累积，不互相覆盖
- `RunState.episodes` 保留全部历史 Episode
- 语义记忆提炼：从多个 Episode 中提取 `decision` / `finding` / `error_pattern` / `project_context`
- 向量嵌入与语义检索（`Episode.embedding` 字段已预留，Phase 1 为 `None`）
- 记忆检索作为工具暴露给 Agent（`memory_search`）

### 2.3 中期：多 Agent 协作

- **Agent 身份模型**：`PrincipalType = USER | AGENT | SYSTEM`
- **Agent 类型**：`planner`, `executor`, `coder`, `reviewer`, `monitor` 等
- **分发机制**：一个 Run 的某一步可委派给子 Agent，子 Agent 以 `AgentPrincipal` 进入 Tool Layer
- **链路隔离**：不同 Agent 类型可绑定不同的 system prompt、工具白名单、上下文窗口策略
- **父 Agent 监督**：子 Agent 的每次工具调用都写入 Event Store，父 Agent 可审查/中断

### 2.4 中期：User / Auth / 权限

- 单用户 + 粗粒度角色（不做 SaaS 多租户）
- JWT 登录，用户 Principal 注入 Run 启动
- `ToolDefinition.required_scope` + `PermissionGuardrail` 在 Tool Layer 强制拦截
- 每个 Event 携带 `principal_id` 和 `principal_type`
- 权限失败直接写入 `GuardrailTriggered` 事件

### 2.5 中期：代码执行 Agent + Docker 沙盒

- 安全执行代码、测试、构建
- Docker 容器隔离
- 代码执行结果作为 `ToolCompleted` 事件写入 Event Store
- 与 `coder` Agent 类型绑定

### 2.6 长期：可观测性与前端

- 事件流可视化时间线（按 seq 渲染，不是简单聊天记录）
- 折叠状态 diff（事件前后 RunState 变化）
- Episode 分段浏览
- 关键决策时间线
- 工具调用链追踪
- 实时 WebSocket 事件流订阅

### 2.7 长期：分析与复盘

- Run 成功率、错误模式、工具调用分布
- Agent 决策质量评估
- 压缩效果分析（original_tokens vs compressed_tokens）
- 成本分析（token 消耗、LLM 调用次数）

---

## 3. 不做 / 边界

- 不做 SaaS 多租户
- 不做复杂 ACL（per-resource 权限）
- 不做前端 Episode 浏览 UI（Phase 1）
- 不做语义记忆跨 Episode 提炼（Phase 1）
- 不做 Agent 自主记忆工具（Phase 1）
- 不做跨 Run 持久化记忆（Phase 1）
- 不做 Docker 沙盒（Phase 1）

---

## 4. 与当前阶段的关系

| 阶段 | 当前状态 | 与本文档的关系 |
|------|---------|--------------|
| v3.0 Phase 1 | 进行中 | 当前唯一应执行的阶段 |
| v3.0 Phase 2 | 未开始 | 本文档 2.2 节，需单独出 PRD |
| User/Auth | 未开始 | 本文档 2.4 节，需单独出 PRD |
| 多 Agent 协作 | 未开始 | 本文档 2.3 节，需单独出 PRD |
| 代码执行沙盒 | 未开始 | 已有 `PRD_v3.2_代码执行Agent_Docker沙盒_pytest.md` |
| 前端可观测性 | 未开始 | 本文档 2.6 节，需单独出 PRD |

---

## 5. 风险提示

1. **事件流架构复杂性**：每新增一个状态字段需要改事件类型、payload、fold、state、测试，开发成本高。
2. **存储膨胀**：每个 thought、每个 tool result、每次压缩都存事件，长期运行需要快照/归档机制。
3. **多 Agent 身份交织**：Agent 调用子 Agent 时，principal 切换和审计链需要仔细设计。
4. **权限模型简化**：粗粒度角色未来若需扩展，迁移成本较高；建议预留 scope 字段但暂不实现 per-resource ACL。

---

## 6. 未作为审查声明

> **本文档记录的是对话中涌现的产品愿景和后续方向，不是经过正式评审的 PRD。**
>
> 任何后续阶段进入开发前，必须：
> 1. 单独编写该阶段的正式 PRD
> 2. 明确前置依赖和验收标准
> 3. 经过用户确认
> 4. 遵守 Harness v2.1 分层约束（L1 → L2 → L3 → L4 → L5 → L6 → L7）

---

*文档生成时间：2026-07-27*
*关联文档：PRD v3.0 Phase 1、PRD v3.2 代码执行 Agent、ARCHITECTURE_v3.0_Phase1.md*
