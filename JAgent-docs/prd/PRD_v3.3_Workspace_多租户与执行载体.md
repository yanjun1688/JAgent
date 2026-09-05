# PRD v3.3: Workspace — 多租户、环境隔离与执行载体

> **版本**: v3.3
> **状态**: 已实现，待产品验收
> **日期**: 2026-08-11
> **产品经理**: （待定）
> **适用组件**: `harness/models/workspace.py`, `harness/storage/*`, `harness/execution/*`, `harness/tools/*`, `harness/api/*`, `frontend/src/*`
> **前置依赖**: Harness v2.1 V0.7.1（任务完成语义分层）
> **决策记录**: 逻辑隔离多租户 / JWT(预留,当前 Header 注入) / Docker 沙箱 / SSH+SFTP 远端 / 分阶段由简到繁
> **实现备注**: Docker/SSH 集成测试和 Monitor 动态租户绑定列为收尾验收项；旧 `.db` 不迁移，必须删除后重建。

---

## 1. 执行摘要

### 1.1 当前问题

JAgent 已有一层名为 `ScopeGuardrail` 的"作用域"机制（V0.4 引入），但在实际使用中**形同虚设**：

| 现状 | 后果 |
|------|------|
| 所有工具绑定 `Guardrail(guardrail_type="scope", config={})`，配置为空 | 边界规则没有真正启用 |
| 文件沙盒根是模块级全局变量 `_SANDBOX_BASE`，serve 启动时设一次 | **所有 Run 共享同一目录**，项目间文件互相可见可写 |
| 无"工具白名单"概念 | Agent 可自由调用任何已注册工具，无项目级限制 |
| 无租户概念 | 单库单用户，不同业务/团队数据混杂 |
| 无执行载体概念 | 所有副作用只能发生在本地进程/本地目录，无法在 Docker 容器或远端服务器执行 |
| 无 workspace 生命周期审计 | 无法回答"谁改过、改成什么、何时改" |

### 1.2 产品目标

> 引入 **Workspace（工作区）** 作为一等实体与 **Tenant（租户）** 的逻辑隔离边界，为每个 workspace 定义**执行载体**（本地目录 / Docker 沙箱 / 远端服务器）与**工具白名单**，并对 workspace 配置变更**全程审计**。

本 PRD 交付一个可上线的**多阶段产品**，按"架构由简到繁"分 P0~P8 落地，每个阶段可独立验收。

### 1.3 关键约束

- 边界强制在**受信组件**完成，Agent 无法绕过（Harness v2.1 约束 4）
- 多租户采用**逻辑隔离**：共享 Event Store，查询层强制按 `tenant_id` 过滤
- 数据结构同源定义：后端 Pydantic Model 为唯一来源，前端经 OpenAPI 生成类型
- **本版本不做**：复杂 RBAC、JWT 认证实现、历史数据库迁移

---

## 2. 用户角色

| 角色 | 描述 | 核心需求 |
|------|------|---------|
| **租户管理员 (Tenant Admin)** | 拥有整租户资源的人 | 租户内 workspace 全生命周期管理、查看审计记录 |
| **运维/操作员 (Operator)** | 管理 workspace 边界的人 | 创建/编辑/停用 workspace；选载体；配工具白名单 |
| **终端用户 (End User)** | 发起任务的用户 | 发任务时选择归属 workspace；看到任务在正确边界内执行 |
| **开发者 (Developer)** | 集成 JAgent 的工程师 | workspace/tenant API 可编程；事件携带 tenant_id/workspace_id 可审计 |
| **审计员 (Auditor)** | 事后追责的人 | 查询 workspace 配置变更历史、谁改的、改了什么 |

---

## 3. 用户故事

| # | 用户故事 | 验收要点 |
|---|---------|---------|
| US-1 | 作为运维，我想为"客户A迁移项目"建独立 workspace，使名下所有 `file_op` 只写进该项目目录 | 不同 workspace 文件互不可见；路径穿越被拦截 |
| US-2 | 作为运维，我想让"只读调研"workspace 仅允许 `browser`/`http_request`，禁止 `file_op` | 白名单外工具被拦截并写入 `GuardrailTriggered` |
| US-3 | 作为租户管理员，我想确保租户 T1 的数据对 T2 完全不可见 | 任何查询在 T1 下无法读到 T2 的 workspace/run/conversation |
| US-4 | 作为终端用户，我发新任务时可选择落在哪个 workspace | `RunStarted` 携带 `workspace_id`；Run 详情展示归属 |
| US-5 | 作为运维，我想让 workspace 的代码/文件在 **Docker 容器** 内读写，不碰宿主机 | sandbox 载体下 file_op 落在容器挂载目录 |
| US-6 | 作为运维，我想让 workspace 直接操作**远端服务器**上的文件 | remote 载体下 file_op 经 SFTP 落到远端路径 |
| US-7 | 作为审计员，我想看 workspace 的创建/更新/删除记录 | 配置每次变更产生审计事件，可查询历史与新旧值 |
| US-8 | 作为终端用户，我想在历史页按 workspace 过滤 Run | 前端可按 workspace 分组/过滤 |
| US-9 | 作为运维，我希望只改文件根/白名单/载体时不需要重建 Run | PATCH 支持部分更新，即时生效 |
| US-10 | 作为开发者，我调用 API 时头部带租户标识即可隔离操作 | `X-Tenant-Id` Header 注入租户上下文（JWT 后续升级） |

---

## 4. 范围声明

### 4.1 本版本做

- **多租户逻辑隔离**：`tenants` 表 + 各业务表 `tenant_id` 列 + `ScopedEventStore` 查询强制过滤 + `X-Tenant-Id` Header 注入（JWT 接口预留）
- **Workspace 实体**：模型、存储、生命周期（CRUD + 停用）
- **执行载体抽象**：`ExecutionTarget`（directory / sandbox / remote）+ `ExecutionBackend` 接口
  - `LocalDirectoryBackend`（本地目录，P2 落地）
  - `DockerSandboxBackend`（P5 落地）
  - `RemoteSSHBackend`（P6 落地）
- **边界强制**：文件隔离（backend.resolve 越界拦截）+ 工具白名单早拦截
- **Run 归属**：`RunStartedPayload.workspace_id` + Run 列按 tenant/workspace 过滤
- **审计事件**：`WorkspaceCreated` / `WorkspaceUpdated` / `WorkspaceDeleted`
- **API**：tenant 感知的 workspace CRUD + run 关联/过滤
- **前端**：Workspace 管理页 + 创建 run 选择 workspace + 历史过滤
- **默认租户/默认 workspace**：serve 启动自动兜底
- **测试**：多租户隔离、载体 backend、审计、白名单、路径安全全覆盖

### 4.2 本版本不做（边界声明）

- ❌ JWT 认证（**预留接口，列为待办**），当前用 `X-Tenant-Id` Header
- ❌ 复杂 RBAC / 细粒度 ACL（仅工具白名单）
- ❌ 历史 Run 数据迁移（当前为开发 Demo；旧 `.db` 不兼容时直接删除并重建）
- ❌ workspace 配置文件级 diff 审计页面（审计事件有，页面后置）
- ❌ 容器网络隔离策略（Docker 网络仅默认 bridge）

### 4.3 开发 Demo 数据策略

- v3.3 以全新数据库 Schema 为准，不提供旧数据库迁移脚本。
- 旧 `.db` 缺少 v3.3 字段或约束时，启动应明确失败并提示删除后重建，不静默猜测历史数据归属。
- 本期不保留旧 `_SANDBOX_BASE`、`set_sandbox_root` 等文件沙盒兼容 API。
- 该策略仅适用于当前无真实用户数据的开发 Demo，不得直接作为生产发布策略。

---

## 5. 数据模型（需求视角）

### 5.1 租户 (F-001)

| 字段 | 类型 | 说明 |
|------|------|------|
| `tenant_id` | str | 主键，如 `t-abc123` / `default` |
| `name` | str | 租户名 |
| `status` | str | `active` / `suspended` |
| `created_at` / `updated_at` | float | 时间戳 |

**关联规则**：一个租户含 0~N 个 Workspace；所有业务数据（workspace/conversation/run 事件）必须归属且仅归属一个租户。

### 5.2 Workspace (F-002)

| 字段 | 类型 | 说明 |
|------|------|------|
| `workspace_id` | str | 主键（uuid 前 8 位） |
| `tenant_id` | str | 所属租户 |
| `name` | str | 显示名，如 "客户A迁移" |
| `description` | str | 描述 |
| `scope.target` | ExecutionTarget | 执行载体（三选一） |
| `scope.allowed_tools` | list[str]\|None | None=不限 / [] =全禁 / 列表=白名单 |
| `status` | str | `active` / `deleted` |
| `created_at` / `updated_at` | float | 时间戳 |

### 5.3 执行载体 ExecutionTarget (F-003)

| 载体 | 关键字段 | 语义 |
|------|---------|------|
| `directory` | `filesystem_root`（绝对路径） | file_op 限制在本地目录内 |
| `sandbox` | `docker_image` + `host_mount_src`（宿主侧目录）+ `mount_root`（容器内路径，默认 `/workspace`） | file_op 只能访问容器内挂载目录，不能访问宿主其他路径 |
| `remote` | `host` + `port(22)` + `username` + `private_key_path` + `remote_root` | file_op 经 SFTP 落到远端路径 |

---

## 6. 功能需求

### 6.1 多租户隔离 (F-004)

- 所有业务查询/写入经由 `ScopedEventStore`（持有 `tenant_id`），自动追加 `WHERE tenant_id=?`；裸 `EventStore` 仅用于启动初始化和基础设施操作
- 租户上下文从 API 中间件读取 `X-Tenant-Id`（缺省 `default`）注入 contextvar
- 受信组件（Scheduler/Executor）经 ScopedEventStore 访问，天然携带租户边界
- 租户管理端点预留（`/api/v1/tenants`），JWT 接入前仅作占位

### 6.2 执行载体抽象 (F-005)

```
ExecutionBackend（接口）
  root()  → 载体根路径
  resolve(path) → 规范化并校验在 root 内，越界抛错
  read / write / append / delete / list
  run_command(cmd, cwd)   # 预留（sandbox/remote 用，directory 后续 run_code）
  close()
```

三个实现各自负责"如何触达目标"：目录 = 本地 os 操作；沙箱 = docker exec/cp；远端 = SFTP/SSH。**tool 层与 guardrail 层只与接口交互，不感知载体差异。**

### 6.3 边界强制 (F-006)

- **文件隔离**：`file_op` 必须绑定 backend，全部路径经 `backend.resolve()` 校验；无 backend 时直接结构化失败，越界 → ScopeGuardrail 拦截
- **工具白名单**：`scope.allowed_tools` 三态（None/[]/列表），未列入工具在 `ToolCall` 之前拦截
- 拦截均写入 `GuardrailTriggered`，payload 携带 `workspace_id`，事件列自动带 `tenant_id`

### 6.4 审计事件 (F-007)

| 事件 | 触发时机 | 关键 payload |
|------|---------|-------------|
| `WorkspaceCreated` | 创建 workspace | workspace_id, tenant_id, name, scope, actor |
| `WorkspaceUpdated` | PATCH 变更任一字段 | workspace_id, tenant_id, changed_fields, old_values, new_values, actor |
| `WorkspaceDeleted` | 停用 workspace | workspace_id, tenant_id, reason, actor |

审计事件以 `workspace_id` 作为事件表 `run_id` 写入（与 conversation 事件先例一致），可用 `GET /api/v1/workspaces/{id}/events` 查询。

### 6.5 Run 关联与过滤 (F-008)

- `CreateRunRequest` 增加 `workspace_id`（可选，缺省 → 租户默认 workspace）
- `RunStartedPayload.workspace_id` + `fold` 出 `RunState.workspace_id`
- `GET /api/v1/runs?workspace_id=` 过滤；租户隔离自动生效

### 6.6 API（F-009）

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/api/v1/workspaces` | 创建（校验载体配置） |
| GET | `/api/v1/workspaces` | 列表（含 run_count） |
| GET | `/api/v1/workspaces/{id}` | 详情 |
| PATCH | `/api/v1/workspaces/{id}` | 更新（校验 allowed_tools 是否已注册） |
| DELETE | `/api/v1/workspaces/{id}` | 停用 + 载体资源清理 |
| GET | `/api/v1/workspaces/{id}/events` | 审计事件查询 |
| POST | `/api/v1/runs` | 创建 Run（可选 workspace_id） |
| GET | `/api/v1/runs?workspace_id=` | 过滤 |
| GET/DELETE | `/api/v1/tenants` | 租户管理（JWT 前置占位） |

### 6.7 前端（F-010）

- `/workspaces` 管理页：列表、详情、载体配置 + 工具白名单编辑、删除确认
- 创建 Run 组件：workspace 下拉（默认"默认工作区"）
- 历史页：按 workspace 过滤 + 展示归属
- Run 详情：显示 workspace 与 tenant 徽标

---

## 7. 优先级（MoSCoW）

| 优先级 | 功能 | 阶段 |
|--------|------|------|
| **Must** | 多租户逻辑隔离、Workspace CRUD、目录载体、工具白名单、文件隔离、Run 归属、审计事件 | P0~P3 |
| **Should** | Docker 沙箱载体、SSH 远端载体、前端管理页、audit 查询 API | P5~P7 |
| **Could** | 载体命令执行（run_command）、租户管理端点 | P8 |
| **Won't(本期)** | JWT 认证、RBAC、历史数据库迁移 | 后续另行设计 |

---

## 8. 决策记录

| # | 决策点 | 方案 | 理由 |
|---|--------|------|------|
| D-1 | workspace 粒度 | 项目/长期任务容器，1:N 承载 Run | 符合主流（VS Code/Codespaces 心智） |
| D-2 | 多租户隔离 | **逻辑隔离**：共享 DB + tenant_id 列 + ScopedEventStore | 改动小、运维简单，符合 TODO_v2.1 §9.2 既有规划 |
| D-3 | 租户身份 | 当前 `X-Tenant-Id` Header；**JWT 预留接口列为待办** | 现有系统无认证，Header 最简；JWT 后续平滑接入 |
| D-4 | 沙箱载体 | **Docker 容器**（docker exec/cp） | 隔离彻底，有 PRD_v3.2 基础 |
| D-5 | 远端载体 | **SSH 密钥 + SFTP** | 主流远程开发模式（VS Code Remote），免密安全 |
| D-6 | 实施节奏 | **分阶段 P0~P8，由简到繁**，每阶段独立验收 | 风险可控，尽早让隔离生效 |
| D-7 | 工具白名单三态 | None=不限 / [] =全禁 / 列表=白名单 | 显式语义，避免默认值歧义 |
| D-8 | 审计事件位置 | 事件流（workspace_id 作 run_id），当前配置仍存表 | 状态靠表、审计靠事件，与 conversation 同构 |

---

## 9. 验收标准

**P0 多租户（Must）**：
- [ ] AC-1：租户 T1 下无法读到 T2 的任何 workspace/conversation/run
- [ ] AC-2：append 自动带 tenant_id，查询自动过滤，无需手动拼条件
- [ ] AC-3：无 `X-Tenant-Id` 时回落 `default`，现有功能不破坏
- [ ] AC-4：全量回归测试通过

**P1~P3 核心（Must）**：
- [ ] AC-5：workspace 可 CRUD，载体配置校验通过才创建
- [ ] AC-6：`../` 越界、绝对路径逃逸、符号链接逃逸全部拦截
- [ ] AC-7：白名单外工具不产生 `ToolCalled`/副作用，写入 `GuardrailTriggered`
- [ ] AC-8：全局 `_SANDBOX_BASE` 移除，无残留依赖
- [ ] AC-9：每次配置变更产生审计事件，old/new 值完整可查

**P5~P7 增强（Should）**：
- [ ] AC-10：sandbox 载体下 file_op 只能访问容器挂载目录，不能访问宿主其他路径
- [ ] AC-11：remote 载体下 file_op 经 SFTP 到达远端路径
- [ ] AC-12：前端可创建/编辑 workspace、按 workspace 过滤历史、Run 显示归属

**P8 收尾（Should）**：
- [ ] AC-13：全量回归通过（以 v3.3 重建后的当前测试基线为准）
- [ ] AC-14：文档（TODO/PRD/ARCHITECTURE/TestPlan）与实际一致

---

## 10. 风险与对策

| 风险 | 等级 | 对策 |
|------|------|------|
| 租户过滤遗漏导致数据串租户 | 高 | ScopedEventStore 作为唯一数据入口 + 专项隔离测试（QA 重点） |
| 符号链接逃逸 workspace/容器/远端根 | 高 | resolve() 真实路径比对 + 每载体专项测试 |
| Docker 不可用的环境 | 中 | backend 检测 docker 可用性，失败时明确报错/skip，不静默 |
| SSH 凭据泄漏 | 中 | 密钥路径而非密钥内容入库；文档明确凭据管理责任 |
| 删除 workspace 误删载体目录 | 中 | 仅允许删除 workspace 专属根；测试覆盖误删路径 |
| contextvar 泄漏到异步任务 | 中 | 遵循 current_run_id 对称 reset + 并发测试 |
| 分阶段引入半成品边界 | 中 | 每阶段独立验收标准，未上线阶段显式声明"未启用" |

---

## 11. 后续规划（待办清单）

- **JWT 认证**：`X-Tenant-Id` 升级为 token 解析（接口已预留）
- **RBAC/ToolACL**：细分权限
- **载体命令执行**：run_command 全面启用 + 白名单
- **workspace 配置 diff 审计页**：前端展示 old/new diff
- **容器沙盒 gVisor**：更强隔离

---

*文档生成：2026-08-11 · 产品角度 · 待审查*
*关联文档：[architecture/ARCHITECTURE_v3.3](../architecture/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md) / [testing/TestPlan_Workspace](../testing/TestPlan_Workspace_v1.0.md) / [plans/TODO_v3.3](../plans/workspace-v3.3/TODO_v3.3_Workspace.md)*
