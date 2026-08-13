"""HarnessAPI dependency container and FastAPI Depends() helpers.

Architecture §8.3.0 — dependency injection pattern:
  - HTTP and WebSocket endpoints receive HarnessAPI through get_hapi() dependency
  - Tests inject mock instances via app.dependency_overrides[get_hapi]
  - configure_hapi() sets the production instance before server start

实时事件流装配：
  HarnessAPI 是粘合层——它持有 Event Store、Executor、Scheduler Registry、
  WebSocket 客户端列表，以及 kernel/tool 配置。
  serve.py 在这里注册 MockKernel 和工具，create_run 通过 start_run() 拉起
  Scheduler 后台循环，事件写入后自动广播到 WebSocket。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fastapi import WebSocket

from harness.core.fold import fold_events
from harness.core.logger import guard_logger
from harness.core.scheduler import PlanningExecutorScheduler, SchedulerConfig
from harness.core.tenant import current_tenant, safe_tenant_dir_component
from harness.models.workspace import ExecutionTarget, ExecutionTargetType, Workspace, WorkspaceScope
from harness.storage.scoped import ScopedEventStore

_log = guard_logger("api.deps")

if TYPE_CHECKING:
    from harness.core.llm_client import LLMClient
    from harness.models.events import Event
    from harness.models.tools import ToolDefinition
    from harness.storage.event_store import EventStore
    from harness.tools.executor import ToolExecutor
    from harness.tools.registry import ToolRegistry


class HarnessAPI:
    """依赖容器：持有 store/executor/scheduler 注册表/WS 客户端/工具配置。

    装配流程（serve.py 调用）：
      1. 创建 HarnessAPI(store, executor)
      2. 设置 tool_defs / tool_fns / llm_client / registry
      3. wire_broadcast() 自动注册 EventStore 写入回调 → WS 推送
      4. create_run 端点调 start_run() → 拉起 V0.7 PlanningExecutorScheduler
    """

    def __init__(self, store: EventStore, executor: ToolExecutor | None = None):
        self._raw_store = store
        self.executor = executor
        self._schedulers: dict[str, PlanningExecutorScheduler] = {}
        self._ws_clients: dict[str, list[WebSocket]] = {}
        self._ws_client_tenants: dict[int, str] = {}
        self._run_backends: dict[str, Any] = {}

        # ── 运行时装配字段（serve.py 设置） ──────────────────────
        self.tool_defs: list[ToolDefinition] = []
        self.tool_fns: dict[str, Callable] = {}
        self.llm_client: LLMClient | None = None
        self.registry: ToolRegistry | None = None
        self.context_manager = None
        self.mcp_manager = None
        self.monitor = None
        self.tracer = None
        self.scheduler_config: SchedulerConfig | None = None

    @property
    def store(self) -> ScopedEventStore:
        """Return a store fixed to the current request/task tenant."""
        return ScopedEventStore(self._raw_store, current_tenant.get())

    @property
    def raw_store(self) -> EventStore:
        return self._raw_store

    async def ensure_default_workspace(self) -> Workspace:
        scoped = self.store
        # P1-A: 每个租户拥有独立的 default 工作区 —— workspace_id 与磁盘根目录
        # 都按租户隔离，杜绝跨租户共享目录或继承他人工作区定义。
        # 仅对默认租户保留旧 "default" 工作区（lifespan 引导创建）的兼容。
        if scoped.tenant_id == "default":
            legacy = await scoped.get_workspace("default")
            if legacy is not None:
                return legacy
        workspace_id = f"default-{scoped.tenant_id}"
        workspace = await scoped.get_workspace(workspace_id)
        if workspace is not None:
            return workspace
        now = time.time()
        root = Path("data/workspaces") / safe_tenant_dir_component(scoped.tenant_id) / "default" / "work"
        workspace = Workspace(
            workspace_id=workspace_id,
            tenant_id=scoped.tenant_id,
            name="default",
            scope=WorkspaceScope(
                target=ExecutionTarget(
                    type=ExecutionTargetType.DIRECTORY,
                    filesystem_root=str(root.resolve()),
                )
            ),
            created_at=now,
            updated_at=now,
        )
        await scoped.ensure_tenant()
        return await scoped.create_workspace(workspace)

    def wire_broadcast(self) -> None:
        """订阅 Event Store 写入通知 → 自动推送给 WebSocket 客户端。"""

        async def _on_event(event: Event) -> None:
            await self.broadcast_event(event.run_id, event.model_dump_json())

        self._raw_store.on_append(_on_event)

    async def start_run(
        self, run_id: str, intent: str, conversation_context: str = "", workspace_id: str | None = None
    ) -> None:
        """创建 PlanningExecutorScheduler 并在后台启动 plan→execute→revise 循环。

        create_run 写入 RunStarted 事件后立即调用此方法。
        Scheduler 以 asyncio.Task 运行在后台，不阻塞 API 响应。

        v2.2+ (JAGENT-2026-P1-13 Bug 6): 若 backend 创建或 scheduler 构造失败，
        立即写结构化 RunFailed，避免"RunStarted 已写但后台从未启动"的孤儿 Run。
        """
        if self.llm_client is None or self.registry is None:
            return

        from harness.core.dag_executor import DagExecutor
        from harness.core.planner import Planner

        scoped_store = self.store
        from harness.tools.executor import ToolExecutor

        try:
            run_executor = ToolExecutor(scoped_store)
            planner = Planner(self.llm_client, self.registry, scoped_store, max_plan_retries=2)
            from harness.execution.factory import create_backend

            workspace = await scoped_store.get_workspace(workspace_id or "default")
            if workspace is None:
                raise ValueError(f"Workspace not found: {workspace_id or 'default'}")
            backend = await create_backend(workspace.scope.target) if workspace else None
            if backend is not None:
                self._run_backends[run_id] = backend
            dag = DagExecutor(run_executor, scoped_store, self.registry, workspace=workspace, backend=backend)
            scheduler = PlanningExecutorScheduler(
                store=scoped_store,
                executor=run_executor,
                planner=planner,
                dag_executor=dag,
                tool_defs=self.tool_defs,
                tool_fns=self.tool_fns,
                config=self.scheduler_config,
                context_manager=self.context_manager,
                monitor=self.monitor,
                tracer=self.tracer,
                workspace=workspace,
                backend=backend,
                run_end_cb=lambda rid: self.cleanup_run_resources(rid),
            )
            self.register_scheduler(run_id, scheduler)
            asyncio.create_task(scheduler.run(run_id, intent, conversation_context=conversation_context))
        except Exception as exc:
            _log.exception("start_run failed for run=%s: %s", run_id, exc)
            try:
                from harness.models.events import EventType, RunFailedPayload

                await scoped_store.append_event(
                    run_id,
                    EventType.RUN_FAILED,
                    RunFailedPayload(
                        final_error=f"Run failed to start: {exc!r}",
                        event_count=1,
                        result_summary=f"Task failed before execution. {exc!r}",
                        user_facing_message="任务未能启动，请检查任务要求或稍后重试。",
                    ).model_dump(),
                )
            except Exception as write_exc:
                _log.exception("Failed to write RunFailed for orphan run=%s: %s", run_id, write_exc)
            raise

    # ── Scheduler 注册 ───────────────────────────────────────

    def register_scheduler(self, run_id: str, scheduler: PlanningExecutorScheduler) -> None:
        self._schedulers[run_id] = scheduler

    def unregister_scheduler(self, run_id: str) -> None:
        self._schedulers.pop(run_id, None)

    def cleanup_run_resources(self, run_id: str) -> None:
        """清理 run 结束后的 API 层资源（scheduler 注册表 + WS 客户端列表 +
        execution backend 容器/SFTP）。同时写入 assistant 消息到关联的 conversation。
        """
        self._schedulers.pop(run_id, None)
        self._ws_clients.pop(run_id, None)
        backend = self._run_backends.pop(run_id, None)
        if backend is not None:
            try:
                asyncio.create_task(backend.close())
            except Exception as exc:
                _log.warning("Failed to close execution backend for run=%s: %s", run_id, exc)
        asyncio.create_task(self._write_assistant_message(run_id))

    async def _write_assistant_message(self, run_id: str) -> None:
        """如果 run 关联了 conversation，写入 ConversationMessage(role=assistant)。"""
        if self._raw_store._conn is None:
            _log.debug(
                "Skipping assistant message for run=%s — EventStore already closed",
                run_id,
            )
            return
        try:
            events = await self.store.get_events(run_id)
            if not events:
                return
            state = fold_events(events)
            if not state.conversation_id:
                return

            conv = await self.store.get_conversation(state.conversation_id)
            if not conv:
                return

            content = "任务已完成。"
            if state.status.value == "failed":
                content = state.user_facing_message or "任务未能完成，请检查任务要求或稍后重试。"
            elif state.summary:
                if isinstance(state.summary, str):
                    content = state.summary
                else:
                    content = str(state.summary)

            from harness.models.events import ConversationMessagePayload, EventType

            await self.store.append_event(
                state.conversation_id,
                EventType.CONVERSATION_MESSAGE,
                ConversationMessagePayload(
                    conversation_id=state.conversation_id,
                    run_id=run_id,
                    role="assistant",
                    content=content,
                ).model_dump(),
            )
            await self.store.increment_message_count(state.conversation_id)
        except Exception as exc:
            _log.exception(
                "Failed to write assistant message for run=%s conversation=%s: %s",
                run_id,
                state.conversation_id if "state" in locals() else None,
                exc,
            )

    # ── WebSocket 广播 ───────────────────────────────────────

    async def broadcast_event(self, run_id: str, event_json: str) -> None:
        """推送一条 JSON 事件给所有订阅该 run 的 WebSocket 客户端。

        遇到异常（客户端断开）时标记为 stale，用 list comprehension 安全移除，
        避免在迭代中修改列表。
        """
        clients = self._ws_clients.get(run_id, [])
        try:
            event_tenant = json.loads(event_json).get("tenant_id", "default")
        except (TypeError, ValueError):
            event_tenant = "default"
        stale: list[WebSocket] = []
        for ws in clients:
            if self._ws_client_tenants.get(id(ws), "default") != event_tenant:
                continue
            try:
                await ws.send_text(event_json)
            except Exception:
                stale.append(ws)
        if stale:
            clients[:] = [w for w in clients if w not in stale]
            for ws in stale:
                self._ws_client_tenants.pop(id(ws), None)


# ── 全局 DI ────────────────────────────────────────────────────


_hapi: HarnessAPI | None = None


def configure_hapi(api: HarnessAPI) -> None:
    """设置生产环境的 HarnessAPI 实例。

    在 serve.py 中创建 HarnessAPI、装配 kernel/tools、wire_broadcast 后调用。
    测试环境用 app.dependency_overrides[get_hapi] 注入 mock 实例。
    """
    global _hapi
    _hapi = api


def get_hapi() -> HarnessAPI:
    """FastAPI 依赖注入函数 —— 端点上使用 Depends(get_hapi) 获取 API 实例。"""
    if _hapi is None:
        raise RuntimeError("HarnessAPI not initialized. Call configure_hapi() first.")
    return _hapi
