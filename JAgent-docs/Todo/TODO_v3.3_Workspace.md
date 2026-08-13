# TODO v3.3: Workspace — 多租户、环境隔离与执行载体（阶段路线图）

> **版本**: v3.3
> **状态**: 主体已实现，待收尾验收
> **基线**: v3.3 采用全新数据库 Schema；旧 `.db` 不迁移，必要时删除后重建
> **执行模式**: 新 session AI 按本文档从 P0 逐阶段推进，每阶段完成跑测试 + 回归全量

---

## 0. 目标与协作约定

### 0.1 当前目标

> 将 V0.4 的粗糙 `ScopeGuardrail` 升级为 **Workspace（一等实体）+ Tenant（多租户逻辑隔离）+ ExecutionTarget（执行载体：本地目录 / Docker 沙箱 / SSH 远端）** 的完整体系，并对 workspace 配置变更全程审计。

### 0.2 如何"无脑执行"

本 TODO 是**唯一入口**。新 session 必须：

1. 按阶段顺序执行（P0 → P1 → ... → P8），**禁止跳跃、禁止并行跨阶段**
2. 每个阶段开始前，先读该阶段标注的文档章节，掌握上下文与设计
3. 每完成一个任务项，跑对应阶段测试；阶段完成后跑当前 v3.3 测试基线全量回归
4. 实现必须遵循 AGENTS.md（受信边界、约束 1/4、分层推进、Pydantic 同源、异步）
5. 完成后更新本文档"完成状态"一栏

### 0.3 软连接（关联文档）

| 文档 | 路径 | 用途 |
|------|------|------|
| 需求文档 (PRD) | [`../Prd/PRD_v3.3_Workspace_多租户与执行载体.md`](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md) | 用户需求、用户故事、验收标准 |
| 开发文档 (ARCHITECTURE) | [`./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md`](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md) | 设计决策、数据模型、调用链、代码范围 |
| 测试计划 (TestPlan) | [`../Test_Plan/TestPlan_Workspace_v1.0.md`](../Test_Plan/TestPlan_Workspace_v1.0.md) | 分阶段用例（WS-T/WS-E/WS-X/WS-F/WS-AU 等） |
| 协作规范 | [`../../AGENTS.md`](../../AGENTS.md) | 项目协作规范（务必遵守） |

> **软连接说明**：上方均为相对路径 Markdown 链接，可在代码库内直接跳转。

---

## 1. 阶段总览

| 阶段 | 名称 | 依赖 | 核心内容 | 验收（PRD AC） |
|------|------|------|---------|---------------|
| P0 | 多租户基建 | 无 | tenants/tenant_id 列 + ScopedEventStore + tenant 中间件 | AC-1~AC-4 |
| P1 | Workspace 实体 + CRUD | P0 | workspace 模型/表/CRUD + run 关联 | AC-5 |
| P2 | Tool Layer 强制 | P1 | current_workspace + ScopeGuardrail(backend) + ToolWhitelist + file_op 重构 + LocalDirectoryBackend | AC-6~AC-8 |
| P3 | 审计事件 | P1 | WorkspaceCreated/Updated/Deleted + 审计查询 | AC-9 |
| P4 | Backend 抽象整合 | P2 | ExecutionBackend 接口泛化 + factory + 审计 API + 前端类型 | AC-13 部分 |
| P5 | Docker 沙箱载体 | P4 | DockerSandboxBackend | AC-10 |
| P6 | SSH 远端载体 | P4 | RemoteSSHBackend | AC-11 |
| P7 | 前端 | P3/P4 | WorkspacePage + 过滤 + RunDetail 徽标 | AC-12 |
| P8 | 收尾 | 全部 | 回归 + 文档同步 + 技术债 | AC-13/14 |

---

## 2. P0 — 多租户基建

**目标**：建立租户逻辑隔离，所有数据访问经 ScopedEventStore，杜绝漏过滤。

**关联文档**：
- 需求：[PRD §5.1 Tenant / §6.1 多租户隔离 / §9 AC-1~AC-4](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §6 多租户 ScopedEventStore](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §6 WS-T-01~09](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T0.1 模型**：新增 `harness/models/workspace.py` 中的 `Tenant` 模型；`harness/models/__init__.py` 导出
- [x] **T0.2 表结构**：`event_store.py` 新增 `tenants` 表；全新 Schema 中 `events/conversations/client_request_claims` 直接包含 `tenant_id` 列；本期不做旧库迁移
- [x] **T0.3 TenantContext**：新增 `harness/core/tenant.py`（`current_tenant` contextvar，默认 `"default"`）
- [x] **T0.4 中间件**：`api/app.py` 或 `deps.py` 增加读取 `X-Tenant-Id` → `current_tenant.set()`（缺省 default）
- [x] **T0.5 ScopedEventStore**：新增 `harness/storage/scoped.py`；`append_event` 自动注入 tenant_id；get/list 系列按 tenant 过滤；workspace 查询已接入
- [x] **T0.6 run 查询租户过滤**：run/conversation/analysis/query/WebSocket 查询统一经 ScopedEventStore
- [x] **T0.7 serve 装配**：启动创建 `default` 租户和 workspace；API 依赖注入经 ScopedEventStore
- [x] **T0.8 受信入口清理**：分析查询、WebSocket、Scheduler、Conversation API 和 Monitor 读写均使用 ScopedEventStore

**验收**：
- WS-T-01~10 通过；当前 v3.3 测试基线全量回归通过
- AC-1~AC-4 达成

---

## 3. P1 — Workspace 实体 + CRUD + Run 关联

**目标**：workspace 成为一等实体（directory 载体先行），Run 带 workspace 归属。

**关联文档**：
- 需求：[PRD §5.2 Workspace / §5.3 ExecutionTarget / §6.5 Run 关联 / §9 AC-5](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §5 数据模型 / §10 API](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §7 WS-E-01~09 / §12 WS-A-01~07,09,10](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T1.1 模型**：`Workspace / WorkspaceScope / ExecutionTarget / WorkspaceUpdate`（三载体字段与 target validator）
- [x] **T1.2 表结构**：新增 `workspaces` 表（scope JSON，UNIQUE(tenant_id,name)）；events 加 `workspace_id` 列
- [x] **T1.3 EventStore CRUD**：workspace CRUD 均按 tenant 过滤
- [x] **T1.4 Run 归属**：`RunStartedPayload.workspace_id` + `fold` 出 `RunState.workspace_id`
- [x] **T1.5 API**：workspace CRUD、Run workspace 字段和 `GET /runs?workspace_id=`
- [x] **T1.5b Conversation 入口**：Conversation message 支持 workspace_id，缺省回落 default workspace
- [x] **T1.6 serve**：创建 default tenant/workspace；`start_run()` 解析 workspace
- [x] **T1.7 OpenAPI**：已重新生成 OpenAPI 和前端 schema

**验收**：
- WS-E-01~09、WS-A-01~07/09/10 通过；全量回归通过
- AC-5 达成

---

## 4. P2 — Tool Layer 强制 + 本地目录载体

**目标**：边界真正生效——文件隔离 + 工具白名单，删除全局 `_SANDBOX_BASE`。

**关联文档**：
- 需求：[PRD §6.3 边界强制 / §9 AC-6~AC-8](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §7.1 ExecutionBackend / §8 调用链 / §9 Tool Layer](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §4 WS-P / §5 WS-G / §8 WS-X / §9 WS-F](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T2.1 ExecutionBackend 接口**：完成统一异步 backend 接口
- [x] **T2.2 LocalDirectoryBackend**：完成路径、符号链接和 parent escape 防护
- [x] **T2.3 factory**：directory/sandbox/remote 分支和明确不可用错误
- [x] **T2.4 ScopeGuardrail 改造**：受信执行路径改走 `backend.resolve()`
- [x] **T2.5 ToolWhitelistGuardrail**：完成 None/[]/列表三态和前置顺序
- [x] **T2.6 executor 注入**：完成 scope/backend/contextvar/partial 注入与 reset
- [x] **T2.7 file_op 重构**：生产执行路径改调 backend；旧测试辅助入口仅保留兼容隔离
- [x] **T2.8 scheduler 装配**：Planning/AgentLoop/DAG 均透传 workspace/backend/scoped store
- [x] **T2.9 GuardrailTriggeredPayload.workspace_id** 贯通

**验收**：
- WS-P-01~08、WS-G-01~09、WS-X-01~07、WS-F-01~06 通过；全量回归通过
- AC-6/7/8 达成
- **注意**：`file_op` 相关旧测试若依赖 `_SANDBOX_BASE` 需同步改测；本期不保留旧沙盒兼容入口

---

## 5. P3 — Workspace 审计事件

**目标**：每次配置变更留痕，可查"谁改了什么、何时改"。

**关联文档**：
- 需求：[PRD §6.4 审计事件 / §9 AC-9](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §5.3 审计事件](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §10 WS-AU-01~07](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T3.1 事件类型**：`EventType` 加 `WORKSPACE_CREATED/UPDATED/DELETED`；`PAYLOAD_MODEL_MAP` 注册 3 个 Payload
- [x] **T3.2 写入点**：create/update/delete 写入审计事件，填充 workspace_id
- [x] **T3.3 查询**：`get_workspace_events(workspace_id)`；list_runs 排除 workspace 审计流
- [x] **T3.4 API**：`GET /api/v1/workspaces/{id}/events`

**验收**：
- WS-AU-01~07 通过；全量回归通过
- AC-9 达成

---

## 6. P4 — Backend 抽象整合 + 审计 API + 类型生成

**目标**：ExecutionBackend 成为唯一执行入口（factory 统一创建），审计查询 API 完善。

**关联文档**：
- 架构：[ARCHITECTURE §7 factory / §6.2 ScopedEventStore](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §11 WS-Y-03 / §12 WS-A-08](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T4.1 factory 完整**：`create_backend(target)` 三载体分支与明确不可用错误
- [x] **T4.2 run 过滤整合**：`list_runs` 支持 tenant + workspace 双条件
- [x] **T4.3 审计 API 完善**：workspace 详情返回 run_count，审计 API 支持 limit/offset
- [x] **T4.4 generate-openapi + TS 类型**：已重新生成并修复生成器的 OpenAPI integer/optional/enum 映射

**验收**：
- WS-A-08、WS-Y-03 通过；全量回归通过

---

## 7. P5 — Docker 沙箱载体

**目标**：sandbox 载体下 file_op 落在容器挂载目录，host 不可见。

**关联文档**：
- 需求：[PRD §5.3 sandbox / §9 AC-10](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §7.2 DockerSandboxBackend / §7.4 不可用策略](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §11 WS-Y-05](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T5.1 pyproject**：采用 Docker CLI，不增加 Python Docker SDK 依赖
- [x] **T5.2 DockerSandboxBackend**：完成挂载、容器 exec 文件操作和路径校验
- [x] **T5.2b 容器生命周期**：backend 负责容器启停/close；Workspace 删除采用软删除，不递归清理载体数据
- [x] **T5.3 可用性探测**：Docker CLI 不存在时抛 `SandboxUnavailableError`
- [ ] **T5.4 测试**：Docker integration test 尚未在 Docker daemon 环境执行

**验收**：
- WS-Y-05 通过（Docker 可用时）；不可用时显式跳过且不破坏其他用例
- AC-10 达成

---

## 8. P6 — SSH 远端载体

**目标**：remote 载体下 file_op 经 SFTP 落到远端路径。

**关联文档**：
- 需求：[PRD §5.3 remote / §9 AC-11](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §7.2 RemoteSSHBackend / §7.4 不可用策略](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §11 WS-Y-06](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T6.1 pyproject**：SSH 依赖按需导入，未安装时明确报错
- [x] **T6.2 RemoteSSHBackend**：完成 SFTP 读写和远端路径校验骨架
- [x] **T6.3 凭据安全**：仅保存 key path，不写入日志和 payload 内容
- [x] **T6.4 测试**：补齐 mock SFTP；真实 SSH 作为环境可用时的可选验收

**验收**：
- WS-Y-06 通过（mock 为主）；AC-11 达成

---

## 9. P7 — 前端

**目标**：workspace 可管理、可按 workspace 过滤、Run 显示归属。

**关联文档**：
- 需求：[PRD §6.7 前端 / §9 AC-12](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §11 前端设计](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §13 WS-C-06 / §14 前端回归](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T7.1 API client**：workspace 类型与请求函数由 OpenAPI 同步
- [x] **T7.2 WorkspacePage**：列表、directory 配置、删除确认
- [x] **T7.3 路由**：注册 `/workspaces` 和 Header 入口
- [x] **T7.4 ChatPage**：创建 Conversation Run 支持 workspace 下拉
- [x] **T7.5 HistoryPage**：workspace 过滤和 RunDetail workspace 徽标均已完成

**验收**：
- WS-C-06 通过；前端人工点检通过
- AC-12 达成

---

## 10. P8 — 收尾

**目标**：全量回归、文档同步、技术债显式记录。

**关联文档**：
- 需求：[PRD §10 风险 / §11 后续规划](../Prd/PRD_v3.3_Workspace_多租户与执行载体.md)
- 架构：[ARCHITECTURE §15 已知技术债](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)
- 测试：[TestPlan §14 回归 / §15 覆盖矩阵](../Test_Plan/TestPlan_Workspace_v1.0.md)

### 任务

- [x] **T8.1 全量回归**：`pytest` 全量通过：952 passed, 2 skipped
- [x] **T8.2 代码规范**：全仓 `ruff check harness tests scripts` 和 `mypy harness` 通过
- [x] **T8.3 文档同步**：已记录 Monitor tenant 边界、WebSocket 安全门、软删除和 fallback 兼容例外
- [x] **T8.4 技术债**：ARCHITECTURE §15 已记录当前接受的技术债和后续边界

**验收**：
- AC-13/14 达成

---

## 11. 完成定义（Definition of Done）

每个阶段必须满足：

- [ ] 该阶段所有 T 任务勾选完成
- [ ] 阶段验收用例全过（见各阶段"验收"）
- [ ] `pytest` 全量通过（以 v3.3 当前测试基线为准）
- [ ] `ruff check` 无告警
- [ ] 未引入跨阶段超前实现
- [ ] 关键代码前置 Guardrail / 校验，无调用处 if 补丁

---

## 12. 后续待办（本期不做，需单独立项）

- [ ] **JWT 认证**：`X-Tenant-Id` 升级为 token 解析（中间件内部实现替换即可）
- [ ] **RBAC/ToolACL**：细分权限
- [ ] **载体 run_command 全面启用** + 命令白名单
- [ ] **workspace 配置 diff 审计页**：前端展示 old/new diff

> 注：网络域名白名单、资源限额已按用户决策 **不列入** 后续规划。

---

*文档更新：2026-08-12 · 主体实现完成，WebSocket 安全门、集成测试和全局 lint/type 收尾中*
*关联文档：Prd/PRD_v3.3_Workspace_多租户与执行载体.md / Dev/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md / Test_Plan/TestPlan_Workspace_v1.0.md*
