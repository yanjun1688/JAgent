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
from typing import TYPE_CHECKING, Callable

from fastapi import WebSocket

from harness.core.scheduler import PlanningExecutorScheduler, SchedulerConfig

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
        self.store = store
        self.executor = executor
        self._schedulers: dict[str, PlanningExecutorScheduler] = {}
        self._ws_clients: dict[str, list[WebSocket]] = {}

        # ── 运行时装配字段（serve.py 设置） ──────────────────────
        self.tool_defs: list[ToolDefinition] = []
        self.tool_fns: dict[str, Callable] = {}
        self.llm_client: LLMClient | None = None
        self.registry: ToolRegistry | None = None
        self.context_manager = None
        self.monitor = None
        self.scheduler_config: SchedulerConfig | None = None

    def wire_broadcast(self) -> None:
        """订阅 Event Store 写入通知 → 自动推送给 WebSocket 客户端。"""
        async def _on_event(event: Event) -> None:
            await self.broadcast_event(event.run_id, event.model_dump_json())
        self.store.on_append(_on_event)

    async def start_run(self, run_id: str, intent: str) -> None:
        """创建 PlanningExecutorScheduler 并在后台启动 plan→execute→revise 循环。

        create_run 写入 RunStarted 事件后立即调用此方法。
        Scheduler 以 asyncio.Task 运行在后台，不阻塞 API 响应。
        """
        if self.llm_client is None or self.registry is None:
            return

        from harness.core.dag_executor import DagExecutor
        from harness.core.planner import Planner

        planner = Planner(self.llm_client, self.registry, self.store, max_plan_retries=2)
        dag = DagExecutor(self.executor, self.store, self.registry)
        scheduler = PlanningExecutorScheduler(
            store=self.store, executor=self.executor,
            planner=planner, dag_executor=dag,
            tool_defs=self.tool_defs, tool_fns=self.tool_fns,
            config=self.scheduler_config,
            context_manager=self.context_manager, monitor=self.monitor,
            run_end_cb=lambda rid: self.cleanup_run_resources(rid),
        )
        self.register_scheduler(run_id, scheduler)
        asyncio.create_task(scheduler.run(run_id, intent))

    # ── Scheduler 注册 ───────────────────────────────────────

    def register_scheduler(self, run_id: str, scheduler: PlanningExecutorScheduler) -> None:
        self._schedulers[run_id] = scheduler

    def unregister_scheduler(self, run_id: str) -> None:
        self._schedulers.pop(run_id, None)

    def cleanup_run_resources(self, run_id: str) -> None:
        """清理 run 结束后的 API 层资源（scheduler 注册表 + WS 客户端列表）。"""
        self._schedulers.pop(run_id, None)
        self._ws_clients.pop(run_id, None)

    # ── WebSocket 广播 ───────────────────────────────────────

    async def broadcast_event(self, run_id: str, event_json: str) -> None:
        """推送一条 JSON 事件给所有订阅该 run 的 WebSocket 客户端。

        遇到异常（客户端断开）时标记为 stale，用 list comprehension 安全移除，
        避免在迭代中修改列表。
        """
        clients = self._ws_clients.get(run_id, [])
        stale = []
        for ws in clients:
            try:
                await ws.send_text(event_json)
            except Exception:
                stale.append(ws)
        if stale:
            clients[:] = [w for w in clients if w not in stale]


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
