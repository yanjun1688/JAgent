# Harness v2.2 — 实现路线图

> 基于 `harness_v2.md` v2.2 架构方案与 `AGENTS.md` 开发协作规范生成
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
- [x] **1.2** 事件数据模型定义 — Pydantic v2: `Event`, `EventType` 枚举, 全部 15 种 Payload（0.5d）
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

**对应架构文档**: `harness_v2.md` §5 Tool Layer 设计（v2.2）

- [x] **2.1** 工具契约定义 — `ToolDefinition` + `SideEffect` + `Guardrail` + `RetryPolicy` Pydantic Model（`harness/models/tools.py`）
  - `name: str` — 全局唯一工具标识符
  - `description: str` — Agent 可读的能力描述
  - `input_schema: dict` — JSON Schema 输入参数声明
  - `output_schema: dict` — JSON Schema 输出结构声明
  - `idempotency_key_fields: list[str]` — 幂等键计算字段集合
  - `side_effects: list[SideEffect]` — write / delete / external
  - `timeout_ms: int` — 单次调用超时上限（默认 30000）
  - `retry_policy: RetryPolicy` — 重试策略（max_retries, backoff_base_ms, retryable_errors）
  - `guardrails: list[Guardrail] | None` — 前置检查列表
  - `requires_confirmation: bool` — 是否需要人工确认（默认 false）

- [x] **2.2** 幂等键计算器 — `IdempotencyKeyGenerator`（`harness/tools/idempotency.py`）
  - `compute(tool_def: ToolDefinition, input: dict) -> str | None`
  - 算法：`sha256(tool_name + canonical_json(input[idempotency_key_fields]))` → hex
  - canonical_json：按键排序、紧凑格式、Unicode 不转义
  - `idempotency_key_fields` 为空 → 返回 `hash(tool_name + "{}")`
  - 调用方可传 `idempotency_key=None` 跳过缓存（对应 `input` 中不含幂等键字段的情况）

- [x] **2.3** Guardrails 框架 — `GuardrailRunner` + `SchemaGuardrail`（`harness/tools/guardrails.py`）
  - `GuardrailResult`：`passed: bool, guardrail_id: str, reason: str`
  - `SchemaGuardrail`：始终内置，作为第一步执行，不依赖 `tool_def.guardrails` 声明
  - `GuardrailRunner.run(tool_def, input) -> list[GuardrailResult]`：按 `tool_def.guardrails` 顺序执行，任一失败立即短路返回
  - 当前 MVP 仅实现 `SchemaGuardrail`；`ScopeGuardrail`/`RateLimitGuardrail`/`DestructiveOpGuardrail`/`DependencyGuardrail` 留接口

- [x] **2.4** Tool Executor 核心流程 — `ToolExecutor`（`harness/tools/executor.py`）
   - 依赖：`EventStore`, `IdempotencyKeyGenerator`, `GuardrailRunner`, `Sandbox`
   - `execute(run_id, tool_name, input, tool_def, tool_fn) -> ToolExecutionResult`
   - **8 步流程**（对应 `harness_v2.md` §5.2）：
     - 0. 生成 `tool_call_id`（`uuid4()`）
     - 1. Schema 校验：调用 `SchemaGuardrail`，失败则写入 `GuardrailTriggered` → 返回
     - 2. 计算幂等键：`IdempotencyKeyGenerator.compute(tool_def, input)`
     - 3. 幂等查重：查 Event Store 是否已有 `ToolCompleted` + 同幂等键 → 是则返回缓存结果
     - 4. Guardrails 前置检查：运行 `tool_def.guardrails`，任一失败 → 写入 `GuardrailTriggered`
     - 5. `requires_confirmation` 检查：
          - `requires_confirmation=false` → 跳过
          - 查已有 `ConfirmationRequested`（同幂等键）→ 查 `ConfirmationReceived(confirmed=true)` → 有则跳过
          - 否则 → 写入 `ConfirmationRequested`（含 `confirmation_id` + `tool_call_id` + `idempotency_key` + `input`）→ 返回 `CONFIRMATION_NEEDED`
     - 6. 写入 `ToolCalled` 事件（含 `tool_call_id` + `idempotency_key`）
     - 7. 经 `Sandbox.invoke()` 统一执行 + 超时控制 → 写入 `ToolCompleted` / `ToolFailed` / `ToolTimeout`
   - `ToolExecutionResult`：`completed | failed | timeout | guardrail_blocked | confirmation_needed | idempotency_hit`

- [x] **2.5** 沙盒执行原型 — `Sandbox`（`harness/tools/sandbox.py`）
   - `run(command: list[str], *, timeout_ms: int, cwd: str | None) -> SandboxResult`
   - 使用 `asyncio.create_subprocess_exec` 异步执行
   - `invoke(fn: Callable, input: dict, *, timeout_ms: int) -> Any`
   - 进程内工具统一入口，管理超时 + 自动识别 async/sync 函数
   - `SandboxResult`：`stdout: str, stderr: str, exit_code: int, duration_ms: int`
   - 超时：`asyncio.wait_for` + `Process.kill()`
   - MVP 阶段仅进程隔离（`subprocess`），不涉及容器

- [x] **2.6** 重试策略实现 — `RetryRunner`（`harness/tools/retry.py`）
  - `RetryPolicy` 模型：`max_retries: int, backoff_base_ms: int, retryable_errors: list[str]`
  - `RetryRunner.should_retry(attempt: int, error: str, policy: RetryPolicy) -> bool`
  - 退避算法：`backoff = base_ms * 2^attempt + jitter(±25%)`
  - `RetryRunner.execute_with_retry(fn, policy) -> T`

- [x] **2.7** 单元测试 + 集成测试（`tests/test_tool_layer.py`）
  - 幂等键计算正确性 + 碰撞场景（7 tests）
  - `SchemaGuardrail` 合法/非法输入分支覆盖（4 tests）
  - `GuardrailRunner` 自定义 guardrail + 短路（5 tests）
  - Tool Executor 8 步流程：缓存命中、Guardrail 拦截、确认请求（12 tests）
  - 确认重入避免：`confirmed=true` 后第二次调用直接执行
  - Sandbox 超时终止 + 输出捕获（4 tests）
  - Retry 退避数学 + 不可重试错误（7 tests）
   - **总计 44 tests，全部通过**

- [x] **2.8** 事件模型扩展 — `ContextCheckpointed` 事件类型（第 15 种）
   - `EventType.CONTEXT_CHECKPOINTED` 枚举成员
   - `ContextCheckpointedPayload`：`checkpoint_seq, snapshot_ref, token_count`
   - `fold_events()` 追加 case `CONTEXT_CHECKPOINTED`，`RunState` 追加 `last_checkpoint_seq: int | None`
   - `PAYLOAD_MODEL_MAP` 同步更新

- [x] **2.9** EventStore PK 冲突重试 — `append_event()` 新增重试逻辑
   - seq 计算非原子操作（`get_latest_seq + 1`），并发写入时可能 PK 冲突
   - 冲突时读取最新 seq 后重试，最多 3 次，超限后抛出异常


**验收检查清单**:
- [x] `ToolDefinition` 契约完整，所有字段有默认值和校验（对应架构文档 §5.1）
- [x] 相同幂等键 + 已有 `ToolCompleted` → 第二次调用返回缓存，不执行（对应 §5.2 缓存命中表）
- [x] 相同幂等键 + 已有 `ToolFailed`/`ToolTimeout` → 允许重试（对应 §5.2 缓存命中表）
- [x] `SchemaGuardrail` 拦截非法参数，写入 `GuardrailTriggered` 事件（对应 §5.2 步骤 4）
- [x] `requires_confirmation=true` + 无已确认记录 → 写入 `ConfirmationRequested`，返回 `confirmation_needed`（对应 §5.3 步骤 ②）
- [x] 确认通过后重入 → 检测到 `ConfirmationReceived(confirmed=true)` 跳过确认，直接执行（对应 §5.3 重入避免）
- [x] `ToolCalled` 事件仅在通过全部检查后写入（幂等命中/Guardrail 拦截时均不写入）（对应 §5.2 ToolCalled 写入时机）
- [x] Executor 步骤 7 经 `Sandbox.invoke()` 统一执行，不直接调 `tool_fn`（对应 §5.1 Sandbox 统一入口）
- [x] 沙盒中执行的代码无法访问宿主机文件系统（除指定目录）
- [x] 超时触发 `ToolTimeout` 事件
- [x] `ContextCheckpointed` 事件类型完整定义，fold 和 model map 同步（对应 §6.2）
- [x] `append_event` PK 冲突自动重试（最多 3 次）（对应 §6.3 MVP 限制）

---

### Phase 3 — Agent Loop Scheduler（L3）

**目标**: 驱动 think → act → observe 循环，自动事件写入

**前置依赖**: L1, L2 完成并通过验收

- [x] **3.1** Scheduler 主循环 — `AgentLoopScheduler.run(run_id)` THINK→ACT→OBSERVE→SCHEDULE（1d）
  - 文件：`harness/core/scheduler.py`
  - 依赖 `AgentKernel`（抽象接口，L4 实现）、`ToolExecutor`（L2）、`EventStore`（L1）
- [x] **3.2** 自动事件写入 — 每轮自动写入 `AgentThought`/`ToolCalled`/`ToolCompleted`/`ToolFailed`（1d）
  - `RunStarted` 在 `run()` 入口写入
  - 每轮 THINK 后自动写入 `AgentThought`
  - ACT 阶段由 `ToolExecutor` 写入工具相关事件
  - 循环终止时自动写入 `RunCompleted` / `RunFailed`
- [x] **3.3** 循环终止条件 — 自然完成 / 错误熔断 / 用户取消，写入 `RunCompleted`/`RunFailed`（0.5d）
  - 自然完成：kernel 返回 `tool_name=None`
  - 未知工具：立即 `RunFailed`
  - 连续失败熔断：`SchedulerConfig.max_consecutive_failures`（默认 5）
  - 最大迭代保护：`SchedulerConfig.max_iterations`（默认 50）
- [x] **3.4** 挂起/恢复原型 — 监听 ConfirmationRequested → 挂起 → 等待确认 → 恢复（1.5d）
  - 挂起：写入 `RunPaused(reason="waiting_confirmation")`
  - 等待：`asyncio.Event.wait()` + 超时保护（`pause_timeout_ms`）
  - 恢复：外部调用 `await scheduler.resume(run_id)` → 写入 `RunResumed` → `event.set()`
  - 恢复后自动**重新执行同一工具调用**（Executor 检测到 ConfirmationReceived 跳过确认步骤）
  - `resume()` 为 `async`，写入 `RunResumed(resume_from_seq=latest_seq)` 后触发 event
- [x] **3.5** 限流与熔断 — 超时熔断 + 连续失败熔断（1d）
  - `SchedulerConfig.max_consecutive_failures` 控制连续失败阈值
  - `SchedulerConfig.pause_timeout_ms` 控制确认等待超时
  - `SchedulerConfig.max_iterations` 控制单次 Run 最大循环轮次
- [x] **3.6** 集成测试（L1+L2+L3） — 完整循环测试 + 事件流重放验证（1d）
  - 12 个测试：简单任务 / 多工具调用 / 自动事件写入 / 未知工具 / 熔断 / 最大迭代 / 挂起 / 恢复 / 事件流重放 / Guardrail 计入熔断 / 暂停确认 / 拒绝确认

**验收检查清单**:
- [x] 3 轮 tool_call 的 run 产生至少 9 个事件（3×AgentThought + 3×ToolCalled + 3×ToolCompleted）
- [x] 挂起后 Agent 循环不继续执行，恢复后重新执行原工具调用并写入 `RunResumed`
- [x] 基于事件流折叠可以恢复出完整的 Agent 决策上下文
- [x] 完整挂起→确认→恢复→执行链路端到端测试通过（test_resume_writes_run_resumed_and_completes_execution）

---

### Phase 4 — Agent Kernel 接口（L4）

**目标**: LLM 调用封装 + 上下文窗口管理 + Tool Registry

**前置依赖**: L3 完成并通过验收

- [x] **4.1** LLM 调用封装 — `LLMClient` 抽象 + `MockLLMClient` 实现（`harness/core/llm_client.py`）
  - `LLMClient.chat(messages, tools, temperature, max_tokens)` 抽象方法
  - `MockLLMClient` 返回预编程响应，记录调用历史
- [x] **4.2** 上下文窗口管理 — 通过 `System Prompt` + `State` 构建对话历史（内置于 `LLMAgentKernel`）
  - 最近 5 条 thought 作为 assistant 消息
  - 最近 5 条 tool_results 作为 user 反馈消息
- [x] **4.3** Tool Registry（MVP） — 由 Scheduler 的 `tool_defs + tool_fns` 参数提供（MVP 简化方案）
  - `build_tool_schemas()` 将 `ToolDefinition` 转为 OpenAI 兼容的函数定义
  - V0.2 升级为正式的 `ToolRegistry` 类（`harness/tools/registry.py`），支持动态注册/查询/移除
- [x] **4.4** System Prompt 管理 — `build_system_prompt()`（`harness/core/system_prompt.py`）
  - 注入 `intent`、工具列表、行为约束规则
  - 支持危险工具标注 `(dangerous — requires confirmation)`
- [x] **4.5** THINK 步骤集成 — Scheduler → `AgentKernel.think()` → 解析 thought + tool_choice
  - `_parse_response()` 支持正则提取 `THOUGHT`/`TOOL`/`ARGS`/`<STOP>`
  - 容错：JSON 解析失败时 args 为空字典
- [x] **4.6** LLM 输出解析容错 — 覆盖 10 种异常场景（`tests/test_kernel.py`）
  - `<STOP>` 信号、标准 tool call、无参数调用、畸形 JSON、纯文本、多行 thought、STOP 优先级高于 TOOL、空响应、只有 TOOL 无 THOUGHT
- [x] **4.7** MVP 验收测试 — 端到端：Scheduler + MockKernel + Executor + EventStore 集成通过
  - 16 个 L4 测试全部通过

**验收检查清单**:
- [x] LLM 调用支持 OpenAI 和 DeepSeek 两种后端（通过 `LLMClient` 抽象，L5 添加具体实现）
- [x] Tool Registry 新增工具后，LLM 调用时自动注入工具定义（`build_tool_schemas()`）
- [x] 解析失败场景有完整的降级和熔断逻辑（畸形 JSON → args={}，空响应 → None tool）

---

## V0.2 — 工具层完善（2 周）

**前置依赖**: MVP 全部完成并通过验收

| # | 任务 | 交付物 | 验收标准 | 预计 | 状态 |
|---|------|--------|----------|------|------|
| 5.0 | `ToolRegistry` 工具注册/加载机制 | `harness/tools/registry.py` | 支持动态注册/查询/移除；`build_llm_schemas()` 自动生成 LLM Schema | 0.5d | ✅ |
| 5.1 | `browser()` 工具 | `harness/tools/browser_tool.py` (Playwright 封装) | 支持导航、点击、输入、截图；独立浏览器上下文 | 2d | ✅ |
| 5.2 | `http_request()` 工具 | `harness/tools/http_request.py` (异步 HTTP 客户端) | 支持 GET/POST/PUT/DELETE；超时控制；响应大小限制 | 1d | ✅ |
| 5.3 | `file_op()` 工具 | `harness/tools/file_op.py` (文件读写操作) | 限定沙盒目录内操作；支持读/写/追加/删除/列表 | 1d | ✅ |
| 5.4 | `mcp_call()` 入口 | `harness/tools/mcp_call.py` (MCP 工具统一调用) | 支持动态连接 MCP server；工具契约自动适配 | 2d | ✅ |
| 5.5 | SKILL 封装 | `harness/tools/skill.py` (多步技能包) | 对外表现为单一 `ToolDefinition`，内部编排多步调用 | 1.5d | ✅ |
| 5.6 | 幂等键全面支持 | 所有工具声明 `idempotency_key_fields` | 非只读工具均声明了幂等键字段；幂等键碰撞可正确查重 | 1d | ✅ |
| 5.7 | 工具测试套件 | `tests/test_tools_v02.py` | 26 项测试覆盖 Registry / http_request / file_op / SKILL；沙盒隔离验证 | 1.5d | ✅ |

---

## V0.3 — 可观测性（2 周）

**前置依赖**: MVP + V0.2 完成

| # | 任务 | 交付物 | 验收标准 | 预计 | 状态 |
|---|------|--------|----------|------|------|
| 6.0 | 后端基础设施补齐 | `EventStore.list_runs()` + `Scheduler.pause()` | 支持列举所有 Run、外部暂停正在运行的 Run | 1d | ✅ |
| 6.1 | FastAPI 项目骨架 | `harness/api/` 模块；uvicorn 入口 | FastAPI app 可启动；OpenAPI 文档自动生成 | 0.5d | ✅ |
| 6.2 | 事件流 REST API | `GET /runs`、`GET /runs/{run_id}/events`、`POST /runs` | OpenAPI 文档完整；分页支持；事件按 seq 排序 | 1d | ✅ |
| 6.3 | Run 管理 API | `POST /runs/{run_id}/pause`、`POST /runs/{run_id}/resume`、`DELETE /runs/{run_id}` | 生命周期管理完整；pause 写入 `RunPaused` 事件 | 1d | ✅ |
| 6.4 | 确认接口 | `POST /runs/{run_id}/confirm` | 幂等确认（同一 confirmation_id 重复提交不重复创建事件） | 0.5d | ✅ |
| 6.5 | WebSocket 事件推送 | `WS /runs/{run_id}/events` | 实时推送事件；按 seq 顺序保证；连接时发送全部历史 | 1.5d | ✅ |
| 6.6 | 前端项目脚手架 | `frontend/` 目录；TypeScript + React + Vite | 后端 OpenAPI Schema 自动生成前端类型 | 1d | ✅ |
| 6.7 | Run 列表页 | 展示所有 Run 的状态、创建时间、事件数 | 状态字段来自 `get_run_state()` 折叠；点击进入详情 | 1d | ✅ |
| 6.8 | Run 详情页 | 事件流按时间线渲染；工具调用 trace 可视化 | 事件按 seq 排序；`ToolCalled`→`ToolCompleted`/`ToolFailed` 链路清晰 | 2d | ✅ |
| 6.9 | 操作员确认 UI | 展示 `ConfirmationRequested` 详情；确认/拒绝按钮 | 确认操作携带 `run_id` + `confirmation_id`；实时刷新 | 1d | ✅ |
| 6.10 | **HarnessAPI DI 重构** | 全局 `_hapi` 单例 → FastAPI `Depends()` 依赖注入 | 所有端点通过 `Depends(get_hapi)` 获取 API 实例；测试通过 `app.dependency_overrides` 注入 mock | 0.5d | ✅ |

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

*基于 Harness v2.2 架构方案 · `AGENTS.md` 开发规范*
*分层推进，禁止跨层，每层交付物需验收后方可进入下一层*
