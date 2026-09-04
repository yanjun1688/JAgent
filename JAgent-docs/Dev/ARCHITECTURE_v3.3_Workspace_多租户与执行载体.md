# ARCHITECTURE v3.3: Workspace — 多租户、环境隔离与执行载体

> **版本**: v3.3
> **前置依赖**: Harness v2.1 V0.7.1（任务完成语义）+ 现有 Conversation 模型
> **基线**: v3.3 全新数据库 Schema；旧 `.db` 不迁移，必要时删除后重建
> **状态**: 已实现，待最终审查
> **关联文档**: Prd/PRD_v3.3_Workspace_多租户与执行载体.md / Test_Plan/TestPlan_Workspace_v1.0.md / Dev/TODO_v3.3_Workspace.md

---

## 1. 目标与范围

### 1.1 目标

将 V0.4 引入的粗糙 `ScopeGuardrail` 升级为一等实体 **Workspace**，并新增 **Tenant（多租户）** 与 **ExecutionTarget（执行载体）**，解决四类架构缺口：

1. **边界静态化**：scope 配置硬编码在 `ToolDefinition.guardrails`（全部 `config={}`），无运行时边界注入
2. **边界全局化**：文件沙盒根是模块级全局变量 `_SANDBOX_BASE`，所有 Run 共享一个目录
3. **边界无载体**：副作用只能发生在本地进程/目录，无法在 Docker 容器或远端服务器执行
4. **边界不可治理**：无租户隔离、无 workspace 存储/API/事件/前端、无配置变更审计

本架构的核心命题：

> **把"边界"从静态工具配置，变为由受信组件（Scheduler）在 Run 启动时注入的运行时对象（Workspace + Tenant + ExecutionTarget），并在 Tool Layer 通过 ExecutionBackend 强制消费。**

### 1.2 范围

- 新增 `harness/models/workspace.py`（Tenant / Workspace / ExecutionTarget / WorkspaceScope）
- 新增 `harness/storage/scoped.py`（ScopedEventStore 逻辑隔离包装）
- 新增 `harness/execution/`（ExecutionBackend 抽象 + 三个实现）
- 改造 `harness/storage/event_store.py`（tenant_id/workspace_id 列 + workspaces/tenants 表 + 审计事件写入）
- 改造 `harness/tools/guardrails.py`（ScopeGuardrail 走 backend.resolve + ToolWhitelist）
- 改造 `harness/tools/executor.py`（current_workspace contextvar + backend 参数注入）
- 改造 `harness/tools/file_op.py`（删除全局 _SANDBOX_BASE，改 backend 驱动）
- 扩展 `harness/models/events.py` / `harness/core/fold.py`（workspace_id 贯通 + 审计事件）
- 扩展 `harness/core/scheduler/*`（workspace/backend 装配）
- 扩展 `harness/api/*`（tenant 注入中间件 + workspace CRUD + run 过滤）
- 扩展前端（Workspace 管理页 + 过滤）

**不做**：JWT（预留接口）、RBAC、历史数据库迁移、容器网络策略。

---

## 2. 架构约束

1. **约束 1**：所有副作用发生在 Tool Layer；workspace 边界强制在 Tool Layer 完成
2. **约束 4**：边界拦截由 Guardrails 负责，**不依赖 Agent 配合**——边界注入经由受信链路（Scheduler → ToolExecutor）
3. **受信边界**：Tenant/Workspace 边界解析是受信行为；`WorkspaceScope` 是受信数据，由 API 层（管理操作）写入
4. **逻辑隔离**：所有业务数据访问经 `ScopedEventStore`；裸 `EventStore` 仅用于启动初始化和基础设施操作，分析查询也必须经过 Scoped 层
5. **同源定义**：Pydantic Model 为唯一来源，前端类型经 OpenAPI 生成
6. **载体透明**：tool 层与 guardrail 层只与 `ExecutionBackend` 接口交互，不感知载体差异

---

## 3. 总体架构与数据层级

```
Tenant（租户）          ← 逻辑隔离边界（tenant_id 列 + ScopedEventStore 过滤）
  └── Workspace          ← 一等实体 + 执行载体（ExecutionTarget）+ 工具白名单
        └── Conversation ← 现有概念，挂"tenant_id + workspace_id"，不变
              └── Run    ← 事件溯源，RunStarted 携带 workspace_id（tenant_id 落事件列）
```

```
┌─ API 层 ────────────────────────────────────────────────┐
│  Middleware: X-Tenant-Id → TenantContext (contextvar)   │
│  workspace CRUD · run CRUD · 审计事件查询                │
├─ 受信组件 ──────────────────────────────────────────────┤
│  ScopedEventStore（自动 WHERE tenant_id=?）             │
│  Scheduler → 装载 Workspace(含 ExecutionTarget)          │
│  ToolExecutor → current_workspace + backend 注入         │
├─ 工具层/执行层 ─────────────────────────────────────────┤
│  ExecutionBackend（directory / docker / ssh-sftp）      │
│  file_op → backend 驱动                                  │
│  ScopeGuardrail → backend.resolve() 越界判定             │
│  ToolWhitelistGuardrail → allowed_tools                 │
├─ 存储 ──────────────────────────────────────────────────┤
│  tenants 表 · workspaces 表(含 target JSON)              │
│  events 表 + tenant_id + workspace_id 列（审计事件流）    │
└─────────────────────────────────────────────────────────┘
```

---

## 4. 关键设计决策

| 决策点 | 方案 | 理由 |
|--------|------|------|
| 多租户隔离 | **逻辑隔离**：共享 Event Store + 业务表 tenant_id 列 + `ScopedEventStore` 强制过滤 | 符合 TODO_v2.1 §9.2 既有规划；改动小、运维简单；无独立库连接管理 |
| 租户身份 | 当前 `X-Tenant-Id` Header → contextvar；`TenantContext` 抽象预留给 JWT | 现有系统无认证，Header 最简；JWT 接入时只替换中间件内部实现 |
| Workspace 存储 | 一等实体 + `workspaces` 表（状态源）；审计事件写入事件流 | 状态靠表、审计靠事件，与 Conversation 同构 |
| 执行载体 | `ExecutionTarget` 三态 + `ExecutionBackend` 接口 + 三实现 | 载体透明：tool 层不感知目录/容器/远端差异 |
| 载体注入 | Scheduler 装配 `backend = factory.create(target)`，经 ToolExecutor 注入 | 与 current_workspace 对称，受信链路显式传入，executor 不自建 I/O 来源 |
| 文件边界判定 | `backend.resolve(path)` 为唯一权威——本地 resolve()、容器内 resolve、远端归一化 | 消除不同载体各自的路径安全实现 |
| 审计事件 | 事件表 `run_id` 列复用 workspace_id（conversation 先例） | 同一事件表、同一 seq 机制、同一 WS 广播 |
| 工具白名单 | `allowed_tools` 三态（None/[]/列表），`ToolWhitelistGuardrail` 自动前置 | 早拦截，白名单外工具副作用为零 |
| 默认兜底 | serve 启动创建 `default` 租户 + `default` workspace | 兼容现状，不破坏现有调用 |
| 历史数据 | 不做迁移 | 旧 `.db` 不兼容时删除后重建，不猜测旧数据归属 |

---

## 5. 数据模型

### 5.1 新增 `harness/models/workspace.py`

```python
"""Tenant + Workspace execution-boundary models."""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ExecutionTargetType(str, Enum):
    DIRECTORY = "directory"
    SANDBOX = "sandbox"
    REMOTE = "remote"


class ExecutionTarget(BaseModel):
    """Where the workspace's side effects land. Exactly one family is set."""
    type: ExecutionTargetType

    # directory
    filesystem_root: str | None = None

    # sandbox (Docker)
    docker_image: str | None = None
    host_mount_src: str | None = None          # 宿主侧挂载源（workspace 专属目录）
    mount_root: str | None = "/workspace"      # 容器内挂载目标

    # remote (SSH + SFTP)
    host: str | None = None
    port: int = 22
    username: str | None = None
    private_key_path: str | None = None         # 只存密钥路径，不存密钥内容
    remote_root: str | None = "/workspace"


class WorkspaceScope(BaseModel):
    target: ExecutionTarget
    allowed_tools: list[str] | None = None   # None=不限 / [] =全禁 / 列表=白名单


class Workspace(BaseModel):
    workspace_id: str
    tenant_id: str
    name: str
    description: str = ""
    scope: WorkspaceScope
    status: Literal["active", "deleted"] = "active"
    created_at: float
    updated_at: float


class Tenant(BaseModel):
    tenant_id: str
    name: str
    status: Literal["active", "suspended"] = "active"
    created_at: float
    updated_at: float


class WorkspaceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    scope: WorkspaceScope | None = None
```

### 5.2 Event Store 扩展

新增表：

```sql
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL,           -- JSON of WorkspaceScope
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (tenant_id, name)
);
```

全新数据库直接创建完整 Schema：`events` / `conversations` / `client_request_claims` 增加 `tenant_id NOT NULL` 列；`events` 增加可空 `workspace_id` 列。`client_request_claims` 的唯一约束为 `(tenant_id, conversation_id, client_request_id)`。本期不提供旧数据库迁移；旧 Schema 启动失败后由开发者删除 `.db` 重建。

### 5.3 审计事件（对应 PRD F-007）

```python
WORKSPACE_CREATED = "WorkspaceCreated"
WORKSPACE_UPDATED = "WorkspaceUpdated"
WORKSPACE_DELETED = "WorkspaceDeleted"

class WorkspaceCreatedPayload(BaseModel):
    workspace_id: str
    tenant_id: str
    name: str
    description: str = ""
    scope: dict
    actor: str = "operator"

class WorkspaceUpdatedPayload(BaseModel):
    workspace_id: str
    tenant_id: str
    changed_fields: list[str]
    old_values: dict
    new_values: dict
    actor: str = "operator"

class WorkspaceDeletedPayload(BaseModel):
    workspace_id: str
    tenant_id: str
    reason: str = ""
    actor: str = "operator"
```

**写入约定**：以 `workspace_id` 作为事件表 `run_id` 写入（照 conversation 事件先例），`workspace_id` 列同步填充，便于 `get_workspace_events()` 查询。

### 5.4 Payload 贯通

```python
class RunStartedPayload(BaseModel):
    intent: str
    current_request: str | None = None
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None
    workspace_id: str | None = None   # 新增

class GuardrailTriggeredPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    guardrail_id: str
    reason: str
    step_id: str | None = None
    workspace_id: str | None = None   # 新增
```

`RunState` 增加 `workspace_id`，`fold_events()` 从 RunStarted 折叠。`tenant_id` 落在事件列（EventsStore 层），不进 RunState Payload 折叠，避免污染状态模型；查询时由 ScopedEventStore 承载。

---

## 6. 多租户（ScopedEventStore）

### 6.1 TenantContext（contextvar）

`harness/core/tenant.py`：

```python
current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar("current_tenant", default="default")
```

FastAPI middleware 读 `X-Tenant-Id`（缺省 `default`）→ `current_tenant.set(...)`。`asyncio.create_task` 自动继承当前上下文，Scheduler 后台任务天然获得租户边界。

### 6.2 ScopedEventStore（`harness/storage/scoped.py`）

```python
class ScopedEventStore:
    """Logical-isolation wrapper: every record carries tenant_id; every query filters by it."""

    def __init__(self, store: EventStore, tenant_id: str):
        self._store = store
        self.tenant_id = tenant_id

    async def append_event(self, run_id, event_type, payload, *, idempotency_key=None, workspace_id=None):
        # tenant_id 写入事件表列（非 payload），受信自动注入，caller 无法伪造
        return await self._store.append_event(
            run_id, event_type, payload,
            idempotency_key=idempotency_key,
            tenant_id=self.tenant_id,
            workspace_id=workspace_id,
        )

    async def get_workspace(self, workspace_id):
        ws = await self._store.get_workspace(workspace_id)
        if ws is None or ws.tenant_id != self.tenant_id:
            return None          # 跨租户访问 → 视为不存在
        return ws

    async def list_workspaces(self):
        return await self._store.list_workspaces(tenant_id=self.tenant_id)

    async def execute_query(self, query_name, params=None):
        # 仅允许受信预定义查询；不接受业务层任意 SQL
        return await self._store.execute_scoped_query(query_name, params, tenant_id=self.tenant_id)

    # 其余方法（run 列表/会话/事件）一律透传 tenant_id 过滤
```

> **实现提示**：`EventStore.append_event` 使用 v3.3 新签名 `append_event(..., *, tenant_id, workspace_id=None, idempotency_key=None, _max_retries=3)`。业务调用统一由 `ScopedEventStore` 注入不可伪造的 tenant_id；旧签名不保留。`conversation_id` 列继续由受信存储逻辑自动填充。

**设计要点**：
- ScopedEventStore 是**唯一数据入口**，杜绝漏过滤（受信组件、API、审计写入都经它）
- `append_event` 受信自动将 `tenant_id` 写入事件表列，caller 无法伪造
- 写路径：tenant_id 写事件列；读路径：业务表按 tenant_id 过滤
- run 列表/详情/会话查询按 `tenant_id` 追加 `WHERE` 条件（EventStore 内部实现，见 §14 阶段说明）
- analysis/query/WebSocket/Scheduler/Monitor 均持有 ScopedEventStore，不得直接调用裸 `EventStore` 或任意 SQL 查询

### 6.3 受信组件持完整权限

Scheduler/Executor 仍持有完整 Writer 权限，但**只能通过当前租户的 ScopedEventStore 实例访问**——租户身份由实例固定，无法在运行中切换。

---

## 7. 执行载体（ExecutionBackend）

### 7.1 新增 `harness/execution/base.py`

```python
class ExecutionBackend(ABC):
    """目标载体统一接口：tool 层不感知目录/容器/远端差异。"""

    @property
    def root(self) -> str: ...

    async def resolve(self, path: str) -> str:
        """规范化路径；越出 root 抛 PermissionError。"""

    async def read(self, path: str) -> str
    async def write(self, path: str, content: str) -> dict
    async def append(self, path: str, content: str) -> dict
    async def delete(self, path: str) -> dict
    async def list(self, path: str) -> dict
    async def run_command(self, cmd: str, cwd: str | None = None) -> SandboxResult  # 预留
    async def close(self) -> None
```

### 7.2 实现（P2/P5/P6 落地）

```python
class LocalDirectoryBackend(ExecutionBackend):
    """本地目录载体：基于 filesystem_root 的本地 IO + resolve() 防逃逸。"""

class DockerSandboxBackend(ExecutionBackend):
    """Docker 容器载体：宿主机 host_mount_src 挂载到容器 mount_root，docker exec 读写。"""

class RemoteSSHBackend(ExecutionBackend):
    """SSH + SFTP 远端载体：SFTP 读写远端 remote_root，SSH exec 执行命令。"""
```

### 7.3 Factory（`harness/execution/factory.py`）

```python
async def create_backend(target: ExecutionTarget) -> ExecutionBackend:
    if target.type == ExecutionTargetType.DIRECTORY:
        return LocalDirectoryBackend(target.filesystem_root)
    if target.type == ExecutionTargetType.SANDBOX:
        return DockerSandboxBackend(image=target.docker_image,
                                    host_src=target.host_mount_src, mount=target.mount_root)
    if target.type == ExecutionTargetType.REMOTE:
        return RemoteSSHBackend(host=target.host, port=target.port, username=target.username,
                                key_path=target.private_key_path, root=target.remote_root)
    raise ValueError(f"Unknown target type: {target.type}")
```

### 7.4 载体不可用策略

- Docker 不存在 / daemon 未启动 → `create_backend` 抛明确错误（`SandboxUnavailableError`），不静默
- 远端 SSH 连接失败 → 同样显式失败；密钥路径不存在 → 创建 workspace 时校验报错

---

## 8. 调用链改造前后对比

### 8.1 现状

```
create_run(API) → HarnessAPI.start_run()
  → PlanningExecutorScheduler.run(...)
    → ToolExecutor.execute(run_id, tool_name, input, tool_def, tool_fn)
      → GuardrailRunner.run(tool_def, input, run_id)
          → ScopeGuardrail.check()  ← 读静态 tool_def 空config + 全局 _SANDBOX_BASE
      → Sandbox.invoke(file_op_fn) → _resolve_path() ← 全局 _SANDBOX_BASE
```

### 8.2 改造后

```
[Middleware] X-Tenant-Id → current_tenant
create_run(body.workspace_id?)
  → scoped = ScopedEventStore(store, tenant_id)
  → ws = scoped.get_workspace(workspace_id 或 default)
  → backend = await create_backend(ws.scope.target)
  → scheduler = PlanningExecutorScheduler(..., workspace=ws, backend=backend, store=scoped)
    → executor = ToolExecutor(scoped, ...)
      → executor.execute(run_id, ..., tool_def, tool_fn,
                    workspace_scope=ws.scope, backend=backend)
        → ① ToolWhitelist 前置检查 ← workspace_scope.allowed_tools
        → ② GuardrailRunner.run(..., workspace_scope, backend)
              → ScopeGuardrail.check() → await backend.resolve(path) 越界判定
        → ③ current_workspace 设置（finally reset）
        → ④ Sandbox.invoke(file_op_fn, input, backend=backend) → backend 执行
```

### 8.3 tool_fn 注入

保留 file_op 执行器签名可独立单测：`ToolExecutor` 用 `functools.partial` 把 `backend` 绑定后调用，工具实现不直接依赖全局 contextvar。

---

## 9. Tool Layer 改造

### 9.1 ScopeGuardrail（backend.resolve）

```python
@staticmethod
async def check(tool_def, input, config, *, backend: ExecutionBackend | None = None):
    if tool_def.name == "file_op":
        path = input.get("path") or ""
        if backend is None:
            return GuardrailResult(False, "scope", "Execution backend is required")
        try:
            await backend.resolve(path)
        except PermissionError as exc:
            return GuardrailResult(False, "scope", str(exc))
    return GuardrailResult(True, "scope", "")
```

### 9.2 ToolWhitelistGuardrail

```python
class ToolWhitelistGuardrail:
    GUARDRAIL_ID = "tool_whitelist"

    @staticmethod
    def check(tool_def, input, config, *, workspace_scope: WorkspaceScope | None = None):
        allowed = workspace_scope.allowed_tools if workspace_scope else None
        if allowed is None:
            return GuardrailResult(True, "tool_whitelist", "")
        if tool_def.name not in allowed:
            return GuardrailResult(
                False, "tool_whitelist",
                f"Tool '{tool_def.name}' not allowed in this workspace",
            )
        return GuardrailResult(True, "tool_whitelist", "")
```

**执行顺序**：`Schema → ToolWhitelist(自动) → depends_on(auto) → tool_def.guardrails`。

### 9.3 file_op（backend 驱动）

删除 `_SANDBOX_BASE` / `set_sandbox_root` / `reset_sandbox_root` / `_resolve_path`，改为强制调用 backend；无 backend 不执行文件操作：

```python
async def file_op_fn(input: dict, *, backend: ExecutionBackend) -> dict:
    operation, path = input["operation"], input["path"]
    if operation == "read":   return await backend.read(path)
    if operation == "write":  return await backend.write(path, input.get("content", ""))
    if operation == "append": return await backend.append(path, input.get("content", ""))
    if operation == "delete": return await backend.delete(path)
    if operation == "list":   return await backend.list(path)
    return {"success": False, "path": path, "error": f"Unknown operation: {operation}"}
```

`ToolDefinition` 保持不变（`guardrails=[destructive, scope]`）。

### 9.4 executor 注入

```python
current_workspace: contextvars.ContextVar = contextvars.ContextVar("current_workspace", default=None)

async def execute(self, run_id, tool_name, input, tool_def, tool_fn, *,
                  workspace_scope=None, backend=None, ...):
    # ① 白名单
    wl = ToolWhitelistGuardrail.check(tool_def, input, {}, workspace_scope=workspace_scope)
    if not wl.passed:
        await self.store.append_event(run_id, GUARDRAIL_TRIGGERED, GuardrailTriggeredPayload(...))
        return ToolExecutionResult(GUARDRAIL_BLOCKED, ...)
    # ② Guardrails（含 Scope via backend）
    gr_results = await self.guardrails.run(tool_def, input, run_id=run_id,
                                           workspace_scope=workspace_scope, backend=backend)
    # ③ 上下文
    token = current_run_id.set(run_id); token_ws = current_workspace.set(workspace_scope)
    try:
        async def _run():
            fn = partial(tool_fn, backend=backend) if tool_name == "file_op" else tool_fn
            return await Sandbox.invoke(fn, input, timeout_ms=tool_def.timeout_ms)
        ...
    finally:
        current_run_id.reset(token); current_workspace.reset(token_ws)
```

---

## 10. API 设计

### 10.1 请求/响应模型（`harness/api/schemas.py`）

```python
class CreateWorkspaceRequest(BaseModel):
    name: str
    description: str = ""
    scope: WorkspaceScope

class WorkspaceResponse(BaseModel):
    workspace_id: str
    tenant_id: str
    name: str
    description: str = ""
    scope: WorkspaceScope
    status: str
    run_count: int = 0
    created_at: float
    updated_at: float

class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceResponse]
    total: int

class CreateRunRequest(BaseModel):   # 扩展
    intent: str
    conversation_id: str | None = None
    client_request_id: str | None = None
    workspace_id: str | None = None
```

### 10.2 路由（`harness/api/routes.py`）

| 端点 | 行为（全部经 ScopedEventStore） |
|------|------|
| `POST /api/v1/workspaces` | 校验载体配置（target 字段完整性 + allowed_tools 工具存在）→ create + 写 WorkspaceCreated 审计 |
| `GET /api/v1/workspaces` | 列表 + run_count 聚合 |
| `GET /api/v1/workspaces/{id}` | 详情（跨租户 → 404） |
| `PATCH /api/v1/workspaces/{id}` | 校验变更 → 写 WorkspaceUpdated 审计（old/new） |
| `DELETE /api/v1/workspaces/{id}` | 软删除 + 载体资源清理 + 写 WorkspaceDeleted 审计 |
| `GET /api/v1/workspaces/{id}/events` | 审计事件查询（workspace_id 事件流） |
| `POST /api/v1/runs` | 可选 workspace_id，缺省 → 租户默认 workspace |
| `GET /api/v1/runs?workspace_id=` | 过滤（list_runs 增 tenant_id + workspace_id 条件） |
| `GET/DELETE /api/v1/tenants` | 租户管理（JWT 前置占位，仅 default 可操作） |

### 10.3 DI 装配（`harness/api/deps.py` / `serve.py`）

- serve 启动：创建 `default` 租户 + `default` workspace（directory 载体，root=`data/workspaces/default/work`）
- `HarnessAPI` 持有基础 EventStore；每次请求由 middleware 组装 `ScopedEventStore(store, tenant_id)`
- `start_run()` 接收 workspace + backend，装配 scheduler

---

## 11. 前端设计

### 11.1 新增 `frontend/src/pages/WorkspacePage.tsx`

- 路由 `/workspaces`（App.tsx 注册 + Header 入口）
- 列表：名称、载体类型徽标、文件根/容器/远端摘要、白名单计数、Run 数、状态
- 详情/编辑：载体配置表单 + allowed_tools 多选 + 删除确认
- 审计 tab：WorkspaceCreated/Updated/Deleted 列表 + old/new 展示

### 11.2 改造

- `ChatPage` 创建 Run 组件：workspace 下拉
- `HistoryPage`：workspace 过滤
- `RunDetail`：workspace 徽标
- `api/client.ts` + `schema.ts`：workspace 相关类型/函数（generate-openapi 生成）

---

## 12. 代码范围清单

### 12.1 后端

| 文件 | 动作 |
|------|------|
| `harness/models/workspace.py` | 新增 |
| `harness/models/__init__.py` | 扩展导出 |
| `harness/models/events.py` | 扩展：workspace_id 贯通 + 3 审计事件 |
| `harness/core/fold.py` | 扩展：RunState.workspace_id |
| `harness/core/tenant.py` | 新增：TenantContext contextvar |
| `harness/storage/event_store.py` | 扩展：全新 Schema + CRUD + 审计写入 + run 过滤 |
| `harness/storage/scoped.py` | 新增：ScopedEventStore |
| `harness/execution/base.py` | 新增：ExecutionBackend ABC |
| `harness/execution/local.py` | 新增：LocalDirectoryBackend |
| `harness/execution/docker.py` | 新增：DockerSandboxBackend |
| `harness/execution/ssh.py` | 新增：RemoteSSHBackend |
| `harness/execution/factory.py` | 新增：create_backend |
| `harness/tools/guardrails.py` | 改造：Scope via backend + ToolWhitelist |
| `harness/tools/executor.py` | 改造：workspace_scope/backend 注入 + 白名单前置 |
| `harness/tools/file_op.py` | 改造：backend 驱动 |
| `harness/core/scheduler/base.py` + loop/plan.py | 扩展：workspace/backend/store 透传 |
| `harness/api/schemas.py` | 扩展 |
| `harness/api/routes.py` | 扩展：workspace CRUD + 审计 + 过滤 |
| `harness/api/deps.py` / `app.py` / `serve.py` | 扩展：middleware + 装配 |
| `pyproject.toml` | 扩展：docker / paramiko（P5/P6 引入） |

### 12.2 前端

| 文件 | 动作 |
|------|------|
| `frontend/src/pages/WorkspacePage.tsx` | 新增 |
| `frontend/src/App.tsx` / `Header.tsx` | 路由 + 入口 |
| `frontend/src/pages/ChatPage.tsx` / `HistoryPage.tsx` / `RunDetail.tsx` | 改造 |
| `frontend/src/api/client.ts` / `schema.ts` | 扩展 |

### 12.3 测试（详见 TestPlan）

`tests/test_tenant.py`（新）/ `tests/test_workspace.py`（新）/ `tests/test_execution.py`（新）/ `tests/test_guardrails_v04.py` / `tests/test_tool_layer.py` / `tests/test_api.py` / `tests/test_scheduler.py` / `tests/test_fold.py` / `tests/test_event_store.py` / `tests/conftest.py`

---

## 13. 权衡分析

### 13.1 好处

| 设计 | 收益 |
|------|------|
| 逻辑隔离 ScopedEventStore | 单入口杜绝漏过滤；无独立库运维成本 |
| ExecutionBackend 抽象 | 三种载体一套 tool 代码；未来新载体只加一个实现 |
| 审计事件 | 每次配置变更留痕，可回答"谁改了什么、何时改" |
| 运行时注入边界 | 隔离可靠、Agent 无法绕过 |
| 四级容器 | 职责正交：租户 vs 边界 vs 上下文 vs 执行 |

### 13.2 坏处与代价

| 代价 | 说明 | 缓解 |
|------|------|------|
| 多一层概念（tenant/workspace/target） | 学习成本 | 文档 + 默认兜底降低首程成本 |
| ScopedEventStore 包装所有访问 | 包裹层变厚 | 仅包装受信/API 入口，内层不复用裸 store |
| contextvar 泄漏风险 | 异步任务忘记 reset | finally 对称 reset + 并发测试 |
| Docker/SSH 依赖 | 新增外部工具依赖 | 每阶段独立生效，不可用时显式报错 |
| 载体资源清理 | DELETE 需清理容器卷/远端路径 | 明确"专属根/挂载"边界，测试覆盖 |

### 13.3 与主流对照

- VS Code Remote / Codespaces：devcontainer 文件 + 工具集 → 对应 target + allowed_tools
- IAM Permission Boundary → workspace 是边界声明者，守卫在 Tool Layer
- SaaS 多租户（共享 schema + Row Level Security）→ ScopedEventStore 近 RLS 语义

---

## 14. 阶段落地（P0~P8）

| 阶段 | 内容 | 关键验收（详见 TestPlan §13 覆盖矩阵） |
|------|------|----------------------------------------|
| P0 | 多租户基建：tenants/tenant_id 列 + ScopedEventStore + middleware | AC-1~AC-4 |
| P1 | Workspace 实体 + 表 + CRUD + run 关联（directory 先行） | AC-5 |
| P2 | Tool Layer：current_workspace + ScopeGuardrail backend + ToolWhitelist + file_op 重构 + LocalDirectoryBackend | AC-6~AC-8 |
| P3 | 审计事件（Created/Updated/Deleted）| AC-9 |
| P4 | ExecutionBackend 接口泛化 + 审计查询 API + 前端类型生成 | AC-9（审计查询） |
| P5 | DockerSandboxBackend | AC-10 |
| P6 | RemoteSSHBackend | AC-11 |
| P7 | 前端 WorkspacePage + 过滤 + RunDetail 徽标 | AC-12 |
| P8 | 收尾回归 + 文档同步 + 技术债记录 | AC-13/AC-14 |

> **顶层入口**：任务执行顺序与验收依赖以 `Dev/TODO_v3.3_Workspace.md` 为准，本表为阶段概要。每阶段完成需跑通对应阶段测试并回归全量。

---

## 15. 已知技术债务（本期接受）

1. 审计事件暂复用 `workspace_id` 作为 `run_id`，Run 查询必须显式排除 Workspace 审计事件；未来可增加独立 stream 类型
2. Header 租户注入不提供身份认证，仅适用于可信 Demo 部署；JWT 后续替换 middleware
3. `run_command` 接口预留但 directory 载体默认不启用 —— 沙箱/远端载体启用时显式声明命令白名单
4. tenant 管理端点仅占位，无真实 JWT —— 接入时替换 middleware 内部实现
5. 生产执行不再使用旧 `_SANDBOX_BASE`；遗留的模块级全局 `_SANDBOX_BASE`、`set_sandbox_root`、`reset_sandbox_root`、`_resolve_path`、`FILE_OP_DEF`、`file_op_fn` 及 `allow_legacy_fallback` 后门均已删除（方案 A 根治），文件访问统一经受信 ExecutionBackend 注入，测试改用 `FileOpTool().to_definition()` 契约 + `LocalDirectoryBackend` 后端；旧 `.db` 删除重建
6. Monitor 由基础 EventStore 接收广播，但读取和反馈写入按当前 task 的 tenant 动态包装 ScopedEventStore；脱离请求上下文的 worker 必须显式携带 tenant_id
7. Workspace DELETE 采用软删除，不递归删除本地目录、Docker mount 或 SSH 远端路径；载体数据清理由独立、显式授权的生命周期操作负责
8. WebSocket 连接必须先通过当前 tenant 的 ScopedEventStore 验证 run_id，未知或跨租户 Run 拒绝订阅

---

*文档生成：2026-08-11 · 架构师角度 · 待审查*
*关联文档：Prd/PRD_v3.3_Workspace_多租户与执行载体.md / Test_Plan/TestPlan_Workspace_v1.0.md / Dev/TODO_v3.3_Workspace.md*
