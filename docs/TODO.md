# Harness v2.1 — 实现路线图

> 基于 `harness_v2.md` 架构方案与 `AGENTS.md` 开发协作规范生成
> 分层推进，每层完成后进入下一层，禁止跨层跳跃

---

## 分层总览

| 层级 | 组件 | 前置依赖 | 里程碑 |
|------|------|----------|--------|
| L1 | Event Store 基础设施 | 无 | MVP Phase 1 |
| L2 | Tool Layer 核心 | L1 | MVP Phase 2 |
| L3 | Agent Loop Scheduler | L1, L2 | MVP Phase 3 |
| L4 | Agent Kernel 接口 | L3 | MVP Phase 4 |
| L5 | 工具注册与实现 | L2 | V0.2 |
| L6 | 接口层（API / WebSocket） | L3 | V0.3 |
| L7 | 前端可观测性 | L6 | V0.3 |

---

## MVP（3 周）

### Phase 1 — Event Store 基础设施（L1）

**目标**: 可工作的 Append-Only 事件存储，支持基本写入和查询

- [x] **1.1** 项目脚手架搭建 — Python 项目结构、pyproject.toml、模块骨架（0.5d）
- [x] **1.2** 事件数据模型定义 — Pydantic v2: `Event`, `EventType` 枚举, 全部 14 种 Payload（0.5d）
- [x] **1.3** SQLite Event Store 实现 — `append_event()`, `get_events()`, `get_event_range()`, `get_latest_seq()`（1d）
- [x] **1.4** 幂等键唯一约束 — SQL 唯一索引 + 重复写入检测（0.5d）
- [x] **1.5** 事件流折叠工具函数 — `fold_events(events) -> RunState` 纯函数（0.5d）
- [x] **1.6** 单元测试 + 集成测试 — 写入路径、折叠逻辑、幂等约束全覆盖（1d）

**验收检查清单**:
- [x] SQLite 表已创建，`PRIMARY KEY (run_id, seq)` 生效
- [x] 尝试 UPDATE/DELETE 报错（物理限制）
- [x] 重复幂等键写入返回同一事件而非新建
- [x] `fold_events()` 可重建任意时刻状态快照

---

### Phase 2 — Tool Layer 核心（L2）

**目标**: 统一工具契约 + 幂等键自动计算 + SchemaGuardrail + 沙盒执行原型

**前置依赖**: L1 完成并通过验收

- [ ] **2.1** 工具契约定义 — `ToolDefinition` Pydantic Model（全部字段 + JSON Schema）（0.5d）
- [ ] **2.2** 幂等键计算器 — `IdempotencyKeyGenerator.compute(tool_def, input) -> str`（0.5d）
- [ ] **2.3** Guardrails 框架 — `GuardrailRunner` + SchemaGuardrail 实现（1d）
- [ ] **2.4** Tool Executor 核心流程 — Schema 校验 → 幂等键 → 查重 → Guardrails → 确认 → 沙盒执行 → 事件写入（1.5d）
- [ ] **2.5** 沙盒执行原型 — `subprocess` 隔离进程 + 超时终止（1d）
- [ ] **2.6** 重试策略实现 — `RetryPolicy` 解析 + 指数退避（0.5d）
- [ ] **2.7** 单元测试 + 集成测试 — Guardrails 分支全覆盖、幂等碰撞、沙盒隔离（1d）

**验收检查清单**:
- [ ] `ToolDefinition` 契约完整，所有字段有默认值和校验
- [ ] 相同幂等键的工具调用第二次不执行，直接返回缓存
- [ ] SchemaGuardrail 拦截非法参数，写入 `GuardrailTriggered` 事件
- [ ] 沙盒中执行的代码无法访问宿主机文件系统（除指定目录）

---

### Phase 3 — Agent Loop Scheduler（L3）

**目标**: 驱动 think → act → observe 循环，自动事件写入

**前置依赖**: L1, L2 完成并通过验收

- [ ] **3.1** Scheduler 主循环 — `AgentLoopScheduler.run(run_id)` THINK→ACT→OBSERVE→SCHEDULE（1d）
- [ ] **3.2** 自动事件写入 — 每轮自动写入 `AgentThought`/`ToolCalled`/`ToolCompleted`/`ToolFailed`（1d）
- [ ] **3.3** 循环终止条件 — 自然完成 / 错误熔断 / 用户取消，写入 `RunCompleted`/`RunFailed`（0.5d）
- [ ] **3.4** 挂起/恢复原型 — 监听 ConfirmationRequested → 挂起 → 等待确认 → 恢复（1.5d）
- [ ] **3.5** 限流与熔断 — 超时熔断 + 连续失败熔断（1d）
- [ ] **3.6** 集成测试（L1+L2+L3） — 完整循环测试 + 事件流重放验证（1d）

**验收检查清单**:
- [ ] 3 轮 tool_call 的 run 产生至少 9 个事件（3×AgentThought + 3×ToolCalled + 3×ToolCompleted）
- [ ] 挂起后 Agent 循环不继续执行，恢复后接续当前工具调用
- [ ] 基于事件流折叠可以恢复出完整的 Agent 决策上下文

---

### Phase 4 — Agent Kernel 接口（L4）

**目标**: LLM 调用封装 + 上下文窗口管理 + Tool Registry

**前置依赖**: L3 完成并通过验收

- [ ] **4.1** LLM 调用封装 — `LLMClient` 抽象 + OpenAI/DeepSeek 实现（1d）
- [ ] **4.2** 上下文窗口管理 — `ContextWindow` 消息管理 + token 计数（0.5d）
- [ ] **4.3** Tool Registry — 运行时工具注册/查询/注销（0.5d）
- [ ] **4.4** System Prompt 管理 — 模板注入 + 工具定义格式化（0.5d）
- [ ] **4.5** THINK 步骤集成 — Scheduler → LLMClient → 解析 thought + tool_choice（0.5d）
- [ ] **4.6** LLM 输出解析容错 — JSON 解析失败重试 + fallback（0.5d）
- [ ] **4.7** MVP 验收测试 — 端到端测试：自然语言 → 工具调用 → 结果（1d）

**验收检查清单**:
- [ ] LLM 调用支持 OpenAI 和 DeepSeek 两种后端
- [ ] Tool Registry 新增工具后，LLM 调用时自动注入工具定义
- [ ] 解析失败场景有完整的降级和熔断逻辑

---

## V0.2 — 工具层完善（2 周）

**前置依赖**: MVP 全部完成并通过验收

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 5.1 | `browser()` 工具 | Playwright 封装 | 支持导航、点击、输入、截图；独立浏览器上下文 | 2d |
| 5.2 | `http_request()` 工具 | 异步 HTTP 客户端 | 支持 GET/POST/PUT/DELETE；超时控制；响应大小限制 | 1d |
| 5.3 | `file_op()` 工具 | 文件读写操作 | 限定沙盒目录内操作；支持读/写/追加/删除 | 1d |
| 5.4 | `mcp_call()` 入口 | MCP 工具统一调用 | 支持动态加载 MCP 工具定义；工具契约自动适配 | 2d |
| 5.5 | SKILL 封装 | 多步技能包原型 | 对外表现为单一 `ToolDefinition`，内部编排多步调用 | 1.5d |
| 5.6 | 幂等键全面支持 | 所有工具声明 `idempotency_key_fields` | 非只读工具均声明了幂等键字段；幂等键碰撞可正确查重 | 1d |
| 5.7 | 工具测试套件 | 每个工具的单元测试 + 集成测试 | 工具分支覆盖 ≥ 85%；沙盒隔离验证 | 1.5d |

---

## V0.3 — 可观测性（2 周）

**前置依赖**: MVP + V0.2 完成

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 6.1 | 事件流 REST API | `GET /runs`、`GET /runs/{run_id}/events`、`POST /runs` | OpenAPI 文档完整；分页支持 | 1d |
| 6.2 | WebSocket 事件推送 | `WS /runs/{run_id}/events` | 实时推送事件；按 seq 顺序保证；断线重连 | 1.5d |
| 6.3 | Run 管理 API | `POST /runs/{run_id}/pause`、`POST /runs/{run_id}/resume`、`DELETE /runs/{run_id}` | 生命周期管理完整 | 1d |
| 6.4 | 确认接口 | `POST /runs/{run_id}/confirm` | 幂等确认（同一 confirmation_id 重复提交不产生副作用） | 0.5d |
| 6.5 | 前端项目脚手架 | TypeScript + React + Vite；OpenAPI 类型自动生成 | 后端模型变更时前端类型自动同步 | 1d |
| 6.6 | Run 列表页 | 展示所有 Run 的状态、创建时间、事件数 | 状态字段来自 `get_run_state()` 折叠 | 1d |
| 6.7 | Run 详情页 | 事件流按时间线渲染；工具调用 trace 可视化 | 事件按 seq 排序；`ToolCalled`→`ToolCompleted`/`ToolFailed` 链路清晰 | 2d |
| 6.8 | 操作员确认 UI | 展示 `ConfirmationRequested` 详情；确认/拒绝按钮 | 确认操作携带 `run_id` + `confirmation_id` | 1d |

---

## V0.4 — Guardrails + 确认流程完善（2 周）

**前置依赖**: V0.3 完成

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 7.1 | ScopeGuardrail | 操作目标范围检查 | 可配置白名单目录/域名/端口 | 1d |
| 7.2 | RateLimitGuardrail | 单位时间调用次数限流 | 可配置每工具/每 run 的调用上限 | 1d |
| 7.3 | DestructiveOpGuardrail | 不可逆操作自动触发确认 | `file_op delete`、`run_code` 等自动标记 `requires_confirmation: true` | 1d |
| 7.4 | DependencyGuardrail | 前置步骤检查 | 通过 Event Store 查询前置事件是否存在 | 1d |
| 7.5 | Guardrail 组合执行 | 多个 Guardrail 顺序执行 + 短路 | 任一 Guardrail 失败即终止，写入 `GuardrailTriggered` | 0.5d |
| 7.6 | 挂起/恢复 UI 完善 | WebSocket 实时推送确认请求；操作员可在页面直接决策 | 确认流程全链路通顺，不丢失上下文 | 1.5d |
| 7.7 | Guardrail 测试 | 每条 Guardrail 规则 100% 分支覆盖 | 包含边界条件、异常输入、组合场景 | 1d |

---

## V0.5 — 长流程稳定性（2 周）

**前置依赖**: V0.4 完成

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 8.1 | Context Manager 实现 | 监控上下文 token 数，接近阈值时触发压缩 | Agent 无感知；压缩后上下文仍保持完整语义 | 2d |
| 8.2 | 滚动摘要策略 | LLM 对历史事件生成摘要，替代原始内容 | 摘要保留关键决策和工具调用结果 | 1.5d |
| 8.3 | Checkpoint 自动触发 | 每 N 轮/每 M tokens 自动写入 `ContextCheckpointed` | 断点续传时从最近 checkpoint 开始恢复，而非从头 | 1d |
| 8.4 | 断点续传完整实现 | Worker 崩溃 → 新 Worker 读取 Event Store → 恢复上下文 → 接续执行 | 恢复时间 < 30s（MVP 可放宽到 60s） | 2d |
| 8.5 | 长流程压力测试 | 100+ 轮工具调用 Run | 不溢出、不丢失上下文、可正常完成或优雅熔断 | 1.5d |

---

## V1.0 — 生产就绪（3 周）

**前置依赖**: V0.5 完成

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 9.1 | PostgreSQL Event Store | 从 SQLite 迁移到 PostgreSQL + JSONB | 分区表支持；GIN 索引加速 payload 查询 | 2d |
| 9.2 | 分布式 Worker | asyncio.Queue → Redis Streams | 多 Worker 可同时消费不同 run_id 的任务 | 3d |
| 9.3 | 分层记忆 | Working / Episodic / Semantic 三层记忆架构 | 短期记忆在上下文窗口；长期记忆持久化 + 按需检索 | 3d |
| 9.4 | 权限系统 | 工具级别权限控制 + API 认证 | 不同角色可见/可调用的工具不同 | 2d |
| 9.5 | 监控与报警 | Prometheus metrics + 关键事件告警 | Worker 健康检查、事件写入延迟、LLM 调用失败率 | 2d |
| 9.6 | 性能优化 | 事件批量写入、查询缓存、上下文窗口预加载 | 事件写入吞吐 ≥ 1000/s；Run 状态查询 < 100ms | 2d |
| 9.7 | 安全审查 | 沙盒隔离加固、敏感信息过滤、审计日志 | 符合生产环境安全基线 | 2d |

---

## MVP 验收标准

```
AgentLoopScheduler 驱动一个 StatefulAgent，完成自然语言任务，
全程事件由系统自动写入（不依赖 Agent 主动触发），
幂等键由 Tool Layer 自动计算，
工具重试无副作用。
```

**验证用例**:
1. 用户输入："搜索 opencode 的 GitHub 仓库，总结其核心功能"
2. Agent 调用 `http_request` 搜索 + `browser` 浏览
3. 完整事件流可重放，重放时工具不重复执行
4. 任意时刻中断后恢复，Agent 能接续执行

---

## 评审检查点（每阶段结束时）

- [ ] 本次实现是否严格属于当前层级，未跨层跳跃？
- [ ] 受信组件的行为是否不依赖 Agent 的配合？
- [ ] 前后端的数据结构是否通过统一 Schema 对齐？
- [ ] 新增事件类型是否已定义并同步到前后端？
- [ ] 是否已编写必要的测试覆盖点？
- [ ] 上下文是否已接近阈值，需要压缩？

---

*基于 Harness v2.1 架构方案 · `AGENTS.md` 开发规范*
*分层推进，禁止跨层，每层交付物需验收后方可进入下一层*
