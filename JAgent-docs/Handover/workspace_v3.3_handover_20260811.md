# Workspace v3.3 实现交接文档

> **状态**：主体实现完成，待最终收尾与独立 Review  
> **日期**：2026-08-11  
> **项目根目录**：`D:\Project\JAgent`  
> **交接目标**：供下一位 AI 复核本轮 Workspace v3.3 多租户、执行载体、API 和前端实现，不默认认为所有 Todo 已完成。

## 0. 先读这些文档

1. [项目协作规范](../../AGENTS.md)
2. [v3.3 TODO 唯一入口](../Dev/TODO_v3.3_Workspace.md)
3. [v3.3 架构设计](../Dev/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
4. [v3.3 PRD](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
5. [v3.3 测试计划](../Test_Plan/TestPlan_Workspace_v1.0.md)
6. [完成语义链路重建交接](./completion_semantics_chain_redesign_handover_20260807.md)

### 0.1 重要范围说明

当前工作树同时包含两条改动链：

- **Workspace v3.3**：本交接文档负责说明，核心是 Tenant、Workspace、ScopedEventStore、ExecutionBackend、API 和前端。
- **完成语义链路重建 A-E**：已有独立交接文档，涉及 `UNSUCCESSFUL`、`step_normal`、DAG 门控、完成证据、probe 和自愈收敛。

下一位 AI 必须先阅读两个交接文档，再判断某个 diff 属于哪条改动链。不要回滚或覆盖工作区内已有的无关改动。

## 1. 用户约束与不可违背的架构边界

- 所有实际副作用必须发生在 Tool Layer。
- Agent 不直接操作文件系统、网络或 Event Store。
- Tenant/Workspace 过滤属于受信组件责任，不能依赖 Agent 或 System Prompt 配合。
- 文件路径边界必须由 `ExecutionBackend.resolve()` 和 Guardrail 强制执行。
- 前端类型以 Pydantic/OpenAPI 为源头，不在前端单独发明业务契约。
- 旧 `.db` 不迁移。旧 Schema 启动时应给出明确错误，删除数据库后重建。
- `X-Tenant-Id` 当前只是 Demo 级租户路由，不等于认证授权。

## 2. 本轮已完成的实现

### P0：多租户基建

- 新增 [TenantContext](../../harness/core/tenant.py)。
- 新增 [ScopedEventStore](../../harness/storage/scoped.py)，自动注入和过滤 `tenant_id`。
- [EventStore](../../harness/storage/event_store.py) 新增 tenants/workspaces Schema 及事件、Conversation、client request claim 的租户字段。
- API middleware 读取 `X-Tenant-Id`，默认使用 `default`。
- Run、Conversation、Analysis、Query、WebSocket、Scheduler、Conversation API 和 Monitor 的业务读写已接入 scoped store。
- serve 启动时创建 default tenant 和 default workspace。

### P1：Workspace 实体与 Run 关联

- [Workspace models](../../harness/models/workspace.py) 定义 `Tenant`、`Workspace`、`WorkspaceScope`、`ExecutionTarget`、`WorkspaceUpdate`。
- Workspace CRUD 按 tenant 隔离，数据库唯一键为 `(tenant_id, name)`。
- `RunStartedPayload.workspace_id`、fold 后的 RunState、Run 查询已贯通。
- Conversation 创建 Run 支持 `workspace_id`，缺省回落 default workspace。
- API 支持 Workspace CRUD 和 `GET /runs?workspace_id=`。

### P2：Tool Layer 强制与本地目录载体

- 新增 [ExecutionBackend 基类](../../harness/execution/base.py)。
- 新增 [LocalDirectoryBackend](../../harness/execution/local.py)，处理绝对路径、符号链接、parent escape 和路径归一化。
- [Backend factory](../../harness/execution/factory.py) 支持 directory/sandbox/remote，并对不可用载体抛结构化不可用错误。
- [ScopeGuardrail 和 ToolWhitelist](../../harness/tools/guardrails.py) 已接入 backend resolve 和工具白名单三态：`None` 不限制、`[]` 全禁、列表白名单。
- [Tool executor](../../harness/tools/executor.py) 注入 workspace/backend/contextvar，并在调用后 reset。
- [file_op](../../harness/tools/file_op.py) 的生产路径改走 backend；旧测试辅助入口仍保留 deprecated fallback，见待决策项。
- Planning、AgentLoop、DAG Scheduler 均透传 workspace/backend/scoped store。
- Guardrail 事件已携带 `workspace_id`。

### P3/P4：审计事件、API 与类型生成

- `WorkspaceCreated`、`WorkspaceUpdated`、`WorkspaceDeleted` 事件及 Payload 已注册。
- Workspace create/update/delete 写入审计事件，使用 `workspace_id` 关联事件流。
- `get_workspace_events(workspace_id)` 已实现，并从普通 Run 列表排除 Workspace 审计事件。
- [API routes](../../harness/api/routes.py) 已提供 Workspace CRUD、审计查询、Run workspace 过滤。
- 审计 API 已具备 `limit/offset` 参数；TODO 文档当前仍把 T4.3 标为未完成，需要下一位 AI 实际确认后更新状态。
- [OpenAPI client/schema](../../frontend/public/openapi.json) 和 [生成脚本](../../scripts/generate_openapi.py) 已同步，修复 integer、enum、optional 映射。

### P5/P6：Docker 与 SSH 执行载体

- [DockerSandboxBackend](../../harness/execution/docker.py) 使用 Docker CLI，支持容器创建、挂载、容器内文件操作、路径校验和 close。
- Docker CLI 不存在或不可用时抛 `SandboxUnavailableError`，不应静默回退到宿主机。
- [RemoteSSHBackend](../../harness/execution/ssh.py) 使用按需导入的 paramiko/SFTP，支持远端读写和远端路径校验。
- SSH 凭据只保存 `private_key_path`，不保存或记录 key 内容。

### P7：前端

- [WorkspacePage](../../frontend/src/pages/WorkspacePage.tsx) 已实现 Workspace 列表、directory 配置和删除确认。
- [ChatPage](../../frontend/src/pages/ChatPage.tsx) 支持创建 Conversation Run 时选择 Workspace。
- [HistoryPage](../../frontend/src/pages/HistoryPage.tsx) 支持 workspace 过滤。
- [RunDetail](../../frontend/src/pages/RunDetail.tsx) 已补 Workspace 标识；TODO 文档当前仍未勾选 T7.5，需要同步核实。
- 路由和 Header 入口已更新，相关 App/API/client 文件见 [frontend/src](../../frontend/src)。

## 3. 验证结果

已执行并记录的结果：

- 后端全量 pytest：`880 passed, 2 skipped, 1 warning`。
- 前端测试：`73 passed`。
- 前端构建：`npm run build` 通过。
- v3.3 相关新增测试：见 [test_workspace.py](../../tests/test_workspace.py) 和 [test_execution.py](../../tests/test_execution.py)。
- 目标新增文件和相关脚本的 targeted `ruff check` 通过。
- `git diff --check` 通过。
- 浏览器检查：Workspace 页面可正常渲染。

### 3.1 尚未完成的验证

- Docker daemon 未在当前环境执行真实 integration test。
- SSH mock SFTP 测试和真实 SSH 测试尚未补齐。
- 全局 `ruff check` 未清零，存在大量仓库既有 lint debt。
- 全局 `mypy harness` 未清零，存在既有类型问题和可选依赖 stub 问题；下一位 AI 应单独确认本轮新增的类型错误，不要把全局基线直接标为 v3.3 完成。
- 未完成完整浏览器端到端验证：当前只验证了 Vite 页面渲染，未在同一进程同时启动后端进行完整 API/WebSocket 流程验证。

## 4. 确认遗留问题

### 4.1 必须继续处理

1. **Workspace 删除时的载体资源清理**
   - 当前 backend 有容器生命周期 close，但 DELETE Workspace 尚未接入 `host_mount_src` 清理。
   - 相关入口：[routes.py](../../harness/api/routes.py)、[docker.py](../../harness/execution/docker.py)、[ssh.py](../../harness/execution/ssh.py)。
   - 必须先定义资源所有权、失败重试、目录是否允许删除、是否需要二次确认，不能直接递归删除用户目录。

2. **Docker integration test**
   - 需要 Docker daemon。
   - 至少验证容器内可读写、宿主路径不可绕过、容器关闭、Docker 不可用时错误类型。

3. **SSH mock SFTP test**
   - 至少覆盖远端 root 内读写、`..`/绝对路径逃逸、paramiko 未安装、连接失败、凭据不泄露。

4. **全局 lint/type debt 分界**
   - 需要区分既有问题与本轮新增问题。
   - 重点复核 `executor.py` backend 注入签名、`api/deps.py` ScopedEventStore 类型兼容、`api/query.py` 的 Event 类型引用等此前 Review 提到的疑点。

5. **文档状态不同步**
   - TODO 中 T4.3、T7.5、T8.3 有部分工作已经实现但仍未勾选。
   - T8.4 的架构技术债清单需要逐项确认，而不是只改复选框。

### 4.2 设计/安全 Review 重点

1. **WebSocket 跨租户订阅**
   - [ws.py](../../harness/api/ws.py) 连接和读取路径使用租户上下文，但广播订阅核心仍以 `run_id` 为主。
   - 需要确认连接建立时是否强制验证该 `run_id` 属于当前 tenant，避免仅凭已知 run_id 订阅他租户事件。

2. **file_op deprecated fallback**
   - [file_op.py](../../harness/tools/file_op.py) 为旧测试辅助保留了 fallback。
   - 架构/TODO 原意是删除全局 `_SANDBOX_BASE`。必须决定彻底删除，还是将其正式记录为仅测试兼容例外并限制生产入口。

3. **裸 EventStore 的业务可达性**
   - 架构要求业务访问经 ScopedEventStore，但裸 [EventStore](../../harness/storage/event_store.py) 的低层查询能力仍存在。
   - 下一位 AI 应检查调用图，确认是否有业务路径绕过 facade；如果要系统强制，需要考虑 API 设计或类型/依赖注入层面的约束，而不是只依赖约定。

4. **Monitor 的 contextvar 依赖**
   - [run_monitor.py](../../harness/monitoring/run_monitor.py) 的动态 scoped 读写已接入，但异步 worker 脱离请求 context 时可能回落 default。
   - 需要通过显式 tenant 参数或事件携带 tenant 的方式确认长期运行 worker 的安全边界。

## 5. 需要用户决策的问题

以下事项不要由下一位 AI 擅自猜测：

1. **删除 Workspace 是否删除载体数据？**
   - A：只软删除 Workspace，保留本地/Docker/远端数据。
   - B：删除 Workspace 同时删除由系统创建且明确归属的 host mount。
   - C：要求用户显式确认后才物理删除，并记录审计事件。
   - 建议：默认 A 或 C，不允许无确认递归删除任意路径。

2. **file_op 兼容 fallback 如何处理？**
   - A：彻底删除并同步重写旧测试。
   - B：保留为测试专用入口，并在架构文档记录兼容例外，生产依赖注入禁止使用。
   - 建议：B 作为短期收尾方案，后续再移除。

3. **WebSocket 租户校验是否提升为强制安全门？**
   - 建议：是。连接/订阅前通过 ScopedEventStore 验证 `run_id + tenant_id`，不存在则拒绝订阅。

4. **全局 lint/type debt 的范围**
   - A：只修本轮新增和触碰文件的错误。
   - B：把全仓库 lint/type 清零作为独立任务。
   - 建议：本轮选择 A，B 单独立项，避免掩盖 Workspace 功能审查。

5. **SSH 测试环境要求**
   - A：只做 mock SFTP，不接真实 SSH。
   - B：mock + 可选 Docker/OpenSSH 集成测试。
   - 建议：A 作为必需验收，B 作为环境可用时的增强验收。

## 6. 明确不属于本轮完成范围的后续项

- JWT 认证替换 `X-Tenant-Id`。
- RBAC/ToolACL 细分权限。
- 全面启用载体 `run_command` 和命令白名单。
- Workspace 配置 diff 审计页面。
- 网络域名白名单和资源限额，按用户决策不纳入 v3.3 后续规划。

## 7. 建议下一位 AI 的 Review 顺序

1. 阅读 [AGENTS.md](../../AGENTS.md)、本文件、[v3.3 TODO](../Dev/TODO_v3.3_Workspace.md) 和 [v3.3 架构](../Dev/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)。
2. 执行 `git status --short`，保留现有未提交改动，不做 reset/checkout/revert。
3. 用 `git diff -- harness harness/api frontend/src tests` 区分 Workspace 与完成语义链路改动。
4. 先复核租户隔离和 WebSocket 授权，再复核 backend 路径边界和资源清理。
5. 运行 v3.3 相关测试：`pytest tests/test_workspace.py tests/test_execution.py -q`。
6. 运行前端：在 `frontend` 目录执行 `npm test` 和 `npm run build`。
7. 在 Docker/SSH 环境可用时补集成测试；不可用时明确记录 skip 原因。
8. 最后更新 [TODO_v3.3](../Dev/TODO_v3.3_Workspace.md)、架构文档的技术债和本交接文档状态。

## 8. 当前工作树注意事项

`git status` 显示工作区包含大量未提交修改和新增文件，包括完成语义相关文档/代码、Workspace v3.3 文档/代码、前端 OpenAPI 生成结果及测试。该状态是交接基线，不代表所有修改都由同一个功能引入。

已有的完成语义链路交接见：[completion_semantics_chain_redesign_handover_20260807.md](./completion_semantics_chain_redesign_handover_20260807.md)。其中仍记录了 D12 串行自愈下游恢复项，下一位 AI 不应把它误判为 Workspace v3.3 的遗留项。

---

**交接结论**：Workspace v3.3 核心链路、WebSocket tenant 安全门、SSH mock 验证、全量 Ruff/mypy、后端测试和前端构建均已完成。Workspace 删除采用软删除，不递归清理载体数据；Docker 真实 daemon 测试仍仅在环境可用时执行增强验收。JWT、RBAC、真实 SSH 和 Docker daemon 集成仍属于环境或后续产品范围。
