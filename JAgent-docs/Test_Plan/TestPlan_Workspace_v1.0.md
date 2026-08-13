# TestPlan: Workspace — 多租户、环境隔离与执行载体

> **版本**: 1.0
> **测试负责人**: QA Engineer
> **基线**: v3.3 全新数据库 Schema；旧 `.db` 不迁移，启动明确失败并提示删除重建
> **实现状态**: 后端租户/workspace/backend/API 与前端管理页已实现；Docker/SSH 集成和 Monitor 动态租户绑定仍是收尾项
> **关联文档**: Prd/PRD_v3.3_Workspace_多租户与执行载体.md / Dev/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md / Dev/TODO_v3.3_Workspace.md
> **测试策略**: 单元测试 → 集成测试 → E2E 测试 → 契约测试 → 回归测试
> **预估新增用例**: 80–100 项
> **测试环境**: Windows 11 + Python 3.12 + pytest + asyncio（Docker/SSH 用例可跳过）

---

## 目录

- [1. 测试策略总览](#1-测试策略总览)
- [2. 测试环境与基础设施](#2-测试环境与基础设施)
- [3. 单元测试 — 模型](#3-单元测试--模型)
- [4. 单元测试 — 路径安全](#4-单元测试--路径安全)
- [5. 单元测试 — Guardrails](#5-单元测试--guardrails)
- [6. 集成测试 — 多租户隔离 (P0)](#6-集成测试--多租户隔离-p0)
- [7. 集成测试 — EventStore workspaces/tenants (P1)](#7-集成测试--eventstore-workspacestenants-p1)
- [8. 集成测试 — Executor 注入与白名单 (P2)](#8-集成测试--executor-注入与白名单-p2)
- [9. 集成测试 — file_op 隔离 (P2)](#9-集成测试--file_op-隔离-p2)
- [10. 集成测试 — 审计事件 (P3)](#10-集成测试--审计事件-p3)
- [11. E2E 测试 — 载体与默认兜底 (P4~P6)](#11-e2e-测试--载体与默认兜底-p4p6)
- [12. API 测试 (P1/P4/P7)](#12-api-测试-p1p4p7)
- [13. 契约测试 — 数据结构一致性](#13-契约测试--数据结构一致性)
- [14. 回归测试](#14-回归测试)
- [15. 验收标准覆盖矩阵](#15-验收标准覆盖矩阵)
- [16. 风险与缓解](#16-风险与缓解)

---

## 1. 测试策略总览

### 1.1 分层结构

```
┌──────────────────────────────────────────────────────────────┐
│  E2E 测试 (test_workspace.py / test_execution.py)            │
│  多租户多 workspace 并行隔离 / 默认兜底 / 载体 backend 链路     │
├──────────────────────────────────────────────────────────────┤
│  API + 集成测试 (test_api.py + test_workspace.py)            │
│  CRUD / run 过滤 / Executor 注入 / file_op 隔离 / 审计事件     │
├──────────────────────────────────────────────────────────────┤
│  单元测试 (test_workspace.py + test_guardrails_v04.py)        │
│  模型 / 路径安全纯函数 / Guardrail 拦截                        │
├──────────────────────────────────────────────────────────────┤
│  契约测试 (EventStore 写入 vs Pydantic Schema)                │
│  RunStarted.workspace_id / Workspace* 审计事件 / target JSON   │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 测试文件映射

```
tests/
├── test_tenant.py             # 新增 — ScopedEventStore 隔离 / middleware
├── test_workspace.py          # 新增 — 模型/CRUD/路径/白名单/审计/E2E
├── test_execution.py          # 新增 — ExecutionBackend 三实现 + factory
├── test_guardrails_v04.py     # 扩展 — Scope via backend / ToolWhitelist
├── test_tool_layer.py         # 扩展 — Executor 注入 / 白名单前置
├── test_api.py                # 扩展 — workspace CRUD + audit + run 过滤
├── test_scheduler.py          # 扩展 — workspace/backend 装配贯通
├── test_fold.py               # 扩展 — RunState.workspace_id
├── test_event_store.py        # 扩展 — 全新 Schema / 表 CRUD / 审计写入
└── conftest.py                # 扩展 — tenant/workspace/backend fixtures
```

### 1.3 关键约束

- EventStore 用 `:memory:` SQLite（集成/E2E）；每个测试使用 v3.3 完整 Schema
- 载体不可用的依赖项必须优雅 skip（Docker 未装、SSH 不可连），**不硬崩**
- 文件隔离测试用 `tmp_path` + 内存 store，不落在真实数据目录
- 不依赖真实 LLM API（MockLLMClient）

---

## 2. 测试环境与基础设施

### 2.1 fixtures（conftest.py 新增）

```python
import pytest_asyncio

@pytest.fixture
def base_store(event_store):
    return event_store

@pytest.fixture
def scoped_a(base_store):
    """租户 A 的 ScopedEventStore。"""
    from harness.storage.scoped import ScopedEventStore
    return ScopedEventStore(base_store, tenant_id="tenant_a")

@pytest.fixture
def scoped_b(base_store):
    return ScopedEventStore(base_store, tenant_id="tenant_b")

@pytest_asyncio.fixture
async def ws_a(scoped_a, tmp_path):
    return await scoped_a.create_workspace(
        name="wsA",
        scope={"target": {"type": "directory",
                          "filesystem_root": f"{tmp_path}/A/work"},
               "allowed_tools": ["file_op", "http_request"]},
    )

@pytest.fixture
def local_backend(tmp_path):
    from harness.execution.local import LocalDirectoryBackend
    return LocalDirectoryBackend(f"{tmp_path}/A/work")
```

### 2.2 辅助函数

- `make_file_op_input(operation, path)`
- `assert_guardrail_blocked(result, guardrail_id)`
- `count_events(store, type, run_id)`
- `skip_if_no_docker()` / `skip_if_no_ssh()`（P5/P6 用例）

---

## 3. 单元测试 — 模型

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-M-01 | ExecutionTarget.directory 正常 | filesystem_root 必填校验 | P0 |
| WS-M-02 | ExecutionTarget.sandbox 正常（docker_image + host_mount_src + mount_root 默认 /workspace） | 默认值正确 | P1 |
| WS-M-03 | ExecutionTarget.remote 正常（host/username/port 默认 22） | 默认值正确 | P1 |
| WS-M-04 | ExecutionTarget 非法型别 | validation error | P1 |
| WS-M-05 | WorkspaceScope.allowed_tools None / [] / 列表 | 三态保留 | P0 |
| WS-M-06 | Workspace 缺省字段默认值 | status=active, description="" | P1 |
| WS-M-07 | WorkspaceUpdate 局部更新 | 只更新给定字段 | P1 |
| WS-M-08 | Tenant 模型 status 枚举校验 | active/suspended | P2 |

## 4. 单元测试 — 路径安全（LocalDirectoryBackend.resolve）

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-P-01 | 相对路径在根内（`a.txt`、`dir/b.txt`） | resolve 返回规范路径 | P0 |
| WS-P-02 | `../` 越界 | PermissionError | P0 |
| WS-P-03 | 绝对路径逃逸（`C:\Windows` / `/etc/passwd`） | PermissionError | P0 |
| WS-P-04 | 深层 `../`（`a/../../x`） | PermissionError | P0 |
| WS-P-05 | 符号链接逃逸：根内软链指向根外 | resolve 后拦截 | P0 |
| WS-P-06 | Windows 分隔符 `\` 与 `/` 归一化 | 判断一致 | P1 |
| WS-P-07 | 根目录自身读写/list | 允许 | P1 |
| WS-P-08 | 根不存在时 write | 自动 mkdir | P1 |

## 5. 单元测试 — Guardrails

### 5.1 ScopeGuardrail（backend 驱动）

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-G-01 | file_op 正常路径 + backend | passed=True | P0 |
| WS-G-02 | file_op 越界路径 + backend | passed=False, guardrail_id="scope" | P0 |
| WS-G-03 | backend=None 时 file_op | 不拦截（瞬时兜底） | P1 |
| WS-G-04 | 非 file_op 工具 + backend | 不过问（return pass） | P1 |

### 5.2 ToolWhitelistGuardrail

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-G-05 | allowed_tools=None → 任意工具 | passed（不限） | P0 |
| WS-G-06 | allowed_tools=["file_op"] 调 http_request | passed=False, guardrail_id="tool_whitelist" | P0 |
| WS-G-07 | allowed_tools=[] → 任意工具 | passed=False（全禁） | P0 |
| WS-G-08 | allowed_tools 含调用工具 | passed=True | P0 |
| WS-G-09 | Runner 顺序：Schema → Whitelist → depends_on | 短路正确 | P1 |

## 6. 集成测试 — 多租户隔离 (P0)

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-T-01 | scoped_a 创建 workspace，scoped_b list 不含它 | 租户隔离 | P0 |
| WS-T-02 | scoped_b get_workspace(跨租户 id) | None（视为不存在） | P0 |
| WS-T-03 | scoped_a append 事件 payload 自动带 tenant_id | 受信注入 | P0 |
| WS-T-04 | 跨租户 run 事件隔离（list_runs 按 tenant 过滤） | 互不可见 | P0 |
| WS-T-05 | 跨租户 conversation 隔离 | 互不可见 | P0 |
| WS-T-06 | 缺省 tenant_id（无 scoped 包装） | 回落 default | P0 |
| WS-T-07 | middleware：X-Tenant-Id 缺省默认 default | 上下文正确 | P1 |
| WS-T-08 | asyncio.create_task 继承租户上下文 | 后台任务不串租户 | P0 |
| WS-T-09 | 多租户并发 append 无串扰 | 事件归属各自租户 | P1 |
| WS-T-10 | analysis/query/WebSocket 通过 ScopedEventStore 查询 | 裸 SQL 或裸 store 不泄漏跨租户数据 | P0 |

## 7. 集成测试 — EventStore workspaces/tenants (P1)

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-E-01 | create_workspace 写入 + scope JSON 往返 | 对象无损 | P0 |
| WS-E-02 | 同租户 name 唯一冲突 | 抛错 | P1 |
| WS-E-03 | get/list/update/delete workspace CRUD | 全通过 | P0 |
| WS-E-04 | delete 软删除 status='deleted' | 状态更新 | P0 |
| WS-E-05 | 全新 Schema 包含 tenant_id/workspace_id 及约束 | 初始化成功且字段约束正确 | P1 |
| WS-E-06 | run_count 聚合 | 计数正确 | P1 |
| WS-E-07 | 默认租户 + 默认 workspace 启动创建 | 存在 | P0 |
| WS-E-08 | list_runs 按 workspace_id 过滤 | 仅返回该 ws run | P1 |
| WS-E-09 | create_workspace 时目录自动创建 | filesystem_root 树存在 | P1 |
| WS-E-10 | 旧 `.db` 缺少 v3.3 Schema | 启动明确失败，删除后可重建 | P1 |

## 8. 集成测试 — Executor 注入与白名单 (P2)

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-X-01 | 白名单拦截发生在 ToolCalled 之前 | 无 ToolCalled/ToolCompleted | P0 |
| WS-X-02 | 拦截写入 GuardrailTriggered | guardrail_id="tool_whitelist" + workspace_id | P0 |
| WS-X-03 | 白名单通过后正常执行 | ToolCalled → ToolCompleted | P0 |
| WS-X-04 | current_workspace contextvar 在 execute 内可见、finally reset | 工具读到；执行后默认 | P0 |
| WS-X-05 | 并发 execute 不同 workspace 不串扰 | 边界各自正确 | P0 |
| WS-X-06 | partial 注入 backend 到 file_op_fn | 工具内 backend 可用 | P0 |
| WS-X-07 | 白名单外工具不计算幂等键/不触发确认 | 拦截早于后续步骤 | P1 |

## 9. 集成测试 — file_op 隔离 (P2)

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-F-01 | A 写入文件，B backend 读不到 | 目录隔离 | P0 |
| WS-F-02 | A 写 `../B/file` 越界 | 拦截，B 目录无变化 | P0 |
| WS-F-03 | read/write/append/delete/list 五操作 | 全部通过 | P0 |
| WS-F-04 | 根不存在自动创建 | mkdir | P1 |
| WS-F-05 | list 只列根内内容 | 不泄漏根外 | P1 |
| WS-F-06 | 全局 _SANDBOX_BASE 不再使用 | 单测不依赖它 | P1 |

## 10. 集成测试 — 审计事件 (P3)

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-AU-01 | create_workspace 写 WorkspaceCreated | payload 含 scope/tenant_id/actor | P0 |
| WS-AU-02 | PATCH 写 WorkspaceUpdated | changed_fields/old/new 正确 | P0 |
| WS-AU-03 | DELETE 写 WorkspaceDeleted | reason/actor 完整 | P0 |
| WS-AU-04 | 审计事件以 workspace_id 为 run_id 写入 | 事件表可查 | P0 |
| WS-AU-05 | 审计事件不进入 run 列表 | list_runs 过滤掉 | P1 |
| WS-AU-06 | 审计查询 API 按租户隔离 | scoped 查询 | P1 |
| WS-AU-07 | 事件 payload 可折叠进 Workspace 历史流 | get_workspace_events | P1 |

## 11. E2E 测试 — 载体与默认兜底 (P4~P6)

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-Y-01 | 两个租户各跑 Plan，文件互不可见 | 各自 completed，隔离 | P0 |
| WS-Y-02 | 不传 workspace_id → 默认 workspace | run 归属 default | P0 |
| WS-Y-03 | directory 载体全链路（create_run → backend → file_op） | 完整事件链 | P0 |
| WS-Y-04 | 白名单禁用工具在 DAG 中调用 | GuardrailTriggered，run 不崩溃 | P1 |
| WS-Y-05 | sandbox 载体文件操作（Docker，skip 不可用） | 容器挂载目录生效 | P1 |
| WS-Y-06 | remote 载体文件操作（SSH，skip 不可用） | SFTP 到达远端路径 | P1 |
| WS-Y-07 | backend 不可用时 create_backend 明确报错 | 抛 SandboxUnavailableError | P1 |
| WS-Y-08 | PATCH 修改载体/白名单即时生效（新 run） | 新边界生效 | P1 |

## 12. API 测试 (P1/P4/P7)

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-A-01 | POST /api/v1/workspaces | 201 + workspace_id | P0 |
| WS-A-02 | POST 非法载体配置 | 400 | P1 |
| WS-A-03 | POST 同租户同名 | 409 | P1 |
| WS-A-04 | GET /api/v1/workspaces | 列表 + run_count | P0 |
| WS-A-05 | GET 详情（含跨租户 404） | 正确 | P0 |
| WS-A-06 | PATCH 更新 + 未注册工具名 400 | 正确 | P0 |
| WS-A-07 | DELETE 删除 + 目录清理 | 200 + status | P0 |
| WS-A-08 | GET /workspaces/{id}/events 审计 | 事件列表 | P1 |
| WS-A-09 | POST /runs 带 workspace_id | RunStarted.workspace_id 正确 | P0 |
| WS-A-10 | GET /runs?workspace_id= | 过滤正确 | P1 |
| WS-A-13 | Conversation message 创建 Run 带 workspace_id | 与 POST /runs 使用同一 workspace 装配链路 | P0 |
| WS-A-11 | GET /tenants / DELETE 占位 | default 可操作 | P2 |
| WS-A-12 | header X-Tenant-Id 变更后 run 列表隔离 | 各自租户 | P0 |

## 13. 契约测试 — 数据结构一致性

| # | 用例 | 预期 | 优先级 |
|---|------|------|--------|
| WS-C-01 | RunStartedPayload.workspace_id 写入读回一致 | 往返一致 | P0 |
| WS-C-02 | GuardrailTriggeredPayload.workspace_id | 结构合法 | P0 |
| WS-C-03 | fold_events → RunState.workspace_id | 一致 | P0 |
| WS-C-04 | WorkspaceCreated/Updated/Deleted payload 合法 | schema 校验 | P0 |
| WS-C-05 | WorkspaceScope JSON → SQLite JSON 列 → 反序列化 | 无损往返 | P0 |
| WS-C-06 | OpenAPI 生成 TS 类型含 workspace 字段 | generate-openapi 可编译 | P1 |

## 14. 回归测试

| 范围 | 关注点 |
|------|--------|
| test_event_store.py | 新表/列、非空 tenant、复合 claim 唯一约束不破坏 events append-only |
| test_guardrails_v04.py | 原 5 guardrail 全通过 |
| test_tool_layer.py | 8 步执行流程不变 |
| test_scheduler.py | workspace/backend 新契约贯通，旧数据库兼容不作为目标 |
| test_api.py | 原 CRUD/pause/resume/confirm 不受影响 |
| test_fold.py | 原 24 事件折叠不受影响 |
| 前端 | workspace 字段缺失时页面不崩溃（"未知"兜底） |
| 全量基线 | v3.3 重建后的当前测试基线全部通过 |

## 15. 验收标准覆盖矩阵

| 验收标准（PRD §9） | 对应测试 |
|--------------------|---------|
| AC-1 租户 T1 读不到 T2 | WS-T-01/02/04/05 |
| AC-2 append 自动带 tenant / 查询自动过滤 | WS-T-03, WS-E-08 |
| AC-3 缺省回落 default | WS-T-06/07, WS-Y-02 |
| AC-4 全量回归 | §14 |
| AC-5 workspace CRUD + 载体校验 | WS-E-01/03, WS-A-01/02/03 |
| AC-6 路径穿越全部拦截 | WS-P-02/03/04/05, WS-F-02 |
| AC-7 白名单外无副作用 + GuardrailTriggered | WS-X-01/02, WS-G-06 |
| AC-8 全局 _SANDBOX_BASE 移除 | WS-F-06 |
| AC-9 配置变更审计完整 | WS-AU-01/02/03/04 |
| AC-10 沙箱载体文件隔离 | WS-Y-05 |
| AC-11 远端载体 SFTP | WS-Y-06 |
| AC-12 前端管理/过滤/徽标 | WS-C-06 + 前端人工点检 |
| AC-13 全量回归 | §14 |
| AC-14 文档一致 | 文档评审 + 阶段验收 |

---

## 16. 风险与缓解（QA 视角）

| 风险 | 缓解 |
|------|------|
| 租户过滤遗漏 → 数据串租户 | ScopedEventStore 单入口 + WS-T-01~05 专项 |
| 符号链接逃逸在 Windows/容器差异 | resolve() + 每载体专项；Docker 用 linux 容器验证 |
| Docker/SSH 环境依赖导致 CI 失败 | skip_if_no_docker/ssh 显式跳过，不可用不硬崩 |
| 目录清理误删 | 仅清理 workspace 专属根；tmp_path 隔离 |
| contextvar 并发串扰 | asyncio.gather 多租户压力用例 WS-T-09 |
| OpenAPI 契约漂移 | 后端模型变更后 generate-openapi + TS 编译 |

---

*文档生成：2026-08-11 · QA 角度 · 待审查*
*关联文档：Prd/PRD_v3.3_Workspace_多租户与执行载体.md / Dev/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md / Dev/TODO_v3.3_Workspace.md*
