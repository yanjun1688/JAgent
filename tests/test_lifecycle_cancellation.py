"""S10 — 生命周期与取消（分阶段超时 + watchdog 回收）测试。

覆盖 D-06（取消+宽限期+迟到断言）、C-03（禁止无限等待）、问题七（资源回收）、
问题八（超时模型粗糙）。分阶段预算、子任务取消、pending_calls 目标。
"""

from __future__ import annotations

import asyncio

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.fold import RunStatus
from harness.core.llm_client import ChatResponse, MockLLMClient
from harness.core.planner import Planner
from harness.core.scheduler.base import SchedulerConfig
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry


class HangingLLM(MockLLMClient):
    """chat 永不返回 — 模拟真实 LLM 挂起。"""

    def __init__(self):
        super().__init__([])
        self.hangs = 0

    async def chat(self, messages, **kwargs):
        self.hangs += 1
        self.calls.append({"messages": messages, "run_id": kwargs.get("run_id")})
        await asyncio.sleep(3600)
        return ChatResponse(content="{}")


async def _make_sched(store, llm, config=None):
    executor = ToolExecutor(store)
    registry = ToolRegistry()
    registry._register(
        ToolDefinition(
            name="echo",
            description="e",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            side_effects=[],
            timeout_ms=2000,
        ),
        lambda x: {"ok": True},
    )
    planner = Planner(llm, registry, store, max_plan_retries=1)
    dag = DagExecutor(executor, store, registry)
    sched = PlanningExecutorScheduler(
        store,
        executor,
        planner,
        dag,
        [],
        {},
        config=config or SchedulerConfig(max_iterations=5),
    )
    return sched


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


# ── 分阶段超时（问题八）─────────────────────────────────────────


async def test_phase_timeout_plan_records_event_and_fails(store):
    sched = await _make_sched(
        store,
        HangingLLM(),
        config=SchedulerConfig(max_iterations=5, run_timeout_ms=250, cancel_grace_ms=100),
    )
    state = await sched.run("r1", "write a file please")
    events = await store.get_events("r1")
    assert state.status == RunStatus.FAILED
    assert any(e.event_type == EventType.RUN_FAILED for e in events)
    # 子任务已回收：_phase_tasks 为空，无卡死
    assert sched._phase_tasks.get("r1") is None or sched._phase_tasks["r1"] == set()


async def test_run_watchdog_converges_hanging_llm(store):
    sched = await _make_sched(
        store,
        HangingLLM(),
        config=SchedulerConfig(max_iterations=5, run_timeout_ms=300),
    )
    state = await sched.run("r1", "fetch a url")
    assert state.status == RunStatus.FAILED
    events = await store.get_events("r1")
    assert any(e.event_type == EventType.RUN_FAILED for e in events)


async def test_external_run_cancellation_reaps_registered_tasks(store):
    sched = await _make_sched(
        store,
        HangingLLM(),
        config=SchedulerConfig(max_iterations=5, run_timeout_ms=60000, cancel_grace_ms=100),
    )
    task = asyncio.create_task(sched.run("r-external-cancel", "fetch a url"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sched._phase_tasks.get("r-external-cancel") is None


# ── 取消 + 宽限期（C-03 / D-06）─────────────────────────────────


async def test_cancel_and_reap_grace_timeout_records_event(store):
    sched = await _make_sched(store, MockLLMClient(responses=[]), config=SchedulerConfig(cancel_grace_ms=100))

    async def stubborn():
        # 吞掉第一次取消，模拟"取消后仍不回收"的子任务（C-03 宽限期场景）
        swallowed = 0
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                asyncio.current_task().uncancel()
                swallowed += 1
                if swallowed >= 2:
                    raise

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0.01)  # 让任务进入睡眠后再取消
    sched._phase_tasks.setdefault("r1", set()).add(task)
    await sched._cancel_and_reap("r1")
    events = await store.get_events("r1")
    cleanup_timeouts = [e for e in events if e.event_type == EventType.TASK_CLEANUP_TIMEOUT]
    assert cleanup_timeouts, "expected TASK_CLEANUP_TIMEOUT structured event (C-03)"
    assert cleanup_timeouts[0].payload["pending_count"] >= 1
    assert cleanup_timeouts[0].payload["grace_ms"] == 100
    assert task.done()  # 强制 cleanup 后任务已回收，不卡死


async def test_cancel_and_reap_clean_tasks_no_event(store):
    sched = await _make_sched(store, MockLLMClient(responses=[]), config=SchedulerConfig(cancel_grace_ms=100))

    async def cooperative():
        await asyncio.sleep(0.01)

    task = asyncio.create_task(cooperative())
    await asyncio.sleep(0.02)  # 已完成
    sched._phase_tasks.setdefault("r1", set()).add(task)
    await sched._cancel_and_reap("r1")
    events = await store.get_events("r1")
    assert not any(e.event_type == EventType.TASK_CLEANUP_TIMEOUT for e in events)


async def test_cancel_and_reap_closes_backend(store):
    from unittest.mock import AsyncMock, MagicMock

    sched = await _make_sched(store, MockLLMClient(responses=[]))
    backend = MagicMock()
    backend.close = AsyncMock()
    sched.backend = backend
    await sched._cancel_and_reap("r1")
    backend.close.assert_awaited_once()


# ── pending_calls 目标（问题七）─────────────────────────────────


async def test_pending_calls_zero_goal_after_cleanup(store):
    from harness.monitoring.run_monitor import RunMonitor

    monitor = RunMonitor(store=store)
    sched = await _make_sched(store, MockLLMClient(responses=[]))
    sched.monitor = monitor
    monitor._pending_calls.setdefault("r1", {})["tc1"] = ("echo", 1, "ep")
    sched.monitor.cleanup("r1")
    assert len(monitor._pending_calls.get("r1", {})) == 0


# ── provider 429/5xx 注入 → 结构化 ToolFailed + retryable ───────


async def test_provider_error_structured_tool_failed(store):
    from harness.models.tools import RetryPolicy
    from harness.tools.executor import ExecutionStatus

    executor = ToolExecutor(store)
    tool_def = ToolDefinition(
        name="flaky",
        description="f",
        input_schema={"type": "object", "properties": {}},
        side_effects=[],
        retry_policy=RetryPolicy(retryable_errors=["429"]),
    )

    def boom(input):
        raise RuntimeError("429 Too Many Requests")

    result = await executor.execute("r1", "flaky", {}, tool_def, boom)
    assert result.status == ExecutionStatus.FAILED
    assert result.retryable is True
    events = await store.get_events("r1")
    failed = [e for e in events if e.event_type == EventType.TOOL_FAILED]
    assert failed
    assert failed[0].payload["retryable"] is True
    assert "429" in failed[0].payload["error"]
