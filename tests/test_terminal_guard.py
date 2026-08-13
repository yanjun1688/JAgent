"""S09 — 终态幂等守卫（_fail/_complete + 迟到事件拦截）测试。

覆盖 C-05（终态幂等是前置）、D-06（终态后无迟到事件）、L-03（拦截只作用于
run 事件流，审计/会话事件不误伤）。任何 Run 最多一个终态事件。
"""

from __future__ import annotations

import asyncio

import pytest

from harness.core.fold import RunStatus, fold_events
from harness.core.scheduler.base import BaseScheduler
from harness.models.events import (
    EventType,
    RunFailedPayload,
    RunStartedPayload,
    ToolCompletedPayload,
    WorkspaceCreatedPayload,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


class _ConcreteScheduler(BaseScheduler):
    """Minimal concrete subclass for exercising base lifecycle guards."""

    async def _run_loop(self, run_id: str, intent: str, conversation_context: str = "") -> object:
        return await self._refresh_state(run_id)


async def _make_sched(store):
    return _ConcreteScheduler(store=store, executor=ToolExecutor(store), tool_defs=[], tool_fns={})


async def _start_run(store, run_id="r1"):
    await store.append_event(run_id, EventType.RUN_STARTED, RunStartedPayload(intent="t").model_dump())
    return run_id


@pytest.fixture
async def sched():
    store = EventStore(":memory:")
    await store.initialize()
    s = await _make_sched(store)
    yield store, s
    await store.close()


async def _count(store, run_id, event_type):
    events = await store.get_events(run_id)
    return sum(1 for e in events if e.event_type == event_type)


# ── _fail 幂等 ───────────────────────────────────────────────────


async def test_fail_twice_writes_single_run_failed(sched):
    store, s = sched
    await _start_run(store)
    await s._fail("r1", "boom")
    await s._fail("r1", "boom again")
    assert await _count(store, "r1", EventType.RUN_FAILED) == 1
    state = fold_events(await store.get_events("r1"))
    assert state.status == RunStatus.FAILED
    assert state.last_error == "boom"


async def test_fail_after_complete_is_ignored(sched):
    store, s = sched
    await _start_run(store)
    await s._complete("r1", "done")
    await s._fail("r1", "too late")
    assert await _count(store, "r1", EventType.RUN_FAILED) == 0
    state = fold_events(await store.get_events("r1"))
    assert state.status == RunStatus.COMPLETED


# ── _complete 幂等 ───────────────────────────────────────────────


async def test_complete_twice_writes_single_run_completed(sched):
    store, s = sched
    await _start_run(store)
    await s._complete("r1", "done")
    await s._complete("r1", "done again")
    assert await _count(store, "r1", EventType.RUN_COMPLETED) == 1
    state = fold_events(await store.get_events("r1"))
    assert state.status == RunStatus.COMPLETED
    assert state.summary == "done"


async def test_complete_after_fail_is_ignored(sched):
    store, s = sched
    await _start_run(store)
    await s._fail("r1", "boom")
    await s._complete("r1", "too late")
    assert await _count(store, "r1", EventType.RUN_COMPLETED) == 0
    state = fold_events(await store.get_events("r1"))
    assert state.status == RunStatus.FAILED


# ── 并发 _fail ───────────────────────────────────────────────────


async def test_concurrent_fail_writes_single_run_failed(sched):
    store, s = sched
    await _start_run(store)
    await asyncio.gather(s._fail("r1", "c1"), s._fail("r1", "c2"), s._fail("r1", "c3"))
    assert await _count(store, "r1", EventType.RUN_FAILED) == 1


# ── 迟到事件拦截（D-06 / L-03）──────────────────────────────────


async def test_late_run_event_rejected_after_terminal(sched):
    store, s = sched
    await _start_run(store)
    await s._fail("r1", "boom")
    result = await s._append_run_event(
        "r1",
        EventType.TOOL_COMPLETED,
        ToolCompletedPayload(tool_call_id="tc1", tool_name="file_op", output={}, duration_ms=1).model_dump(),
    )
    assert result is None  # 被拒，不写入
    assert await _count(store, "r1", EventType.TOOL_COMPLETED) == 0
    assert await _count(store, "r1", EventType.LATE_EVENT_REJECTED) == 1
    rejected = [e for e in await store.get_events("r1") if e.event_type == EventType.LATE_EVENT_REJECTED][0]
    assert rejected.payload["event_type"] == EventType.TOOL_COMPLETED.value
    assert rejected.payload["reason"] == "run already terminal"


async def test_run_completed_event_after_terminal_not_rejected(sched):
    """终态事件本身由 _fail/_complete 幂等守卫处理，不经过迟到拦截。"""
    store, s = sched
    await _start_run(store)
    await s._fail("r1", "boom")
    await s._append_run_event("r1", EventType.RUN_FAILED, RunFailedPayload(final_error="x", event_count=1).model_dump())
    assert await _count(store, "r1", EventType.RUN_FAILED) == 1  # 仍只 1 个


# ── L-03：审计/会话事件不受误伤 ─────────────────────────────────


async def test_workspace_audit_event_unaffected_after_terminal(sched):
    store, s = sched
    await _start_run(store)
    await s._fail("r1", "boom")
    # workspace 审计事件经 EventStore 直写（同一表 run_id=workspace_id），不受守卫影响
    await store.append_event(
        "ws-1",
        EventType.WORKSPACE_CREATED,
        WorkspaceCreatedPayload(
            workspace_id="ws-1", tenant_id="t", name="n", scope={}
        ).model_dump(),
        is_audit=True,
    )
    ws_events = await store.get_events("ws-1")
    assert any(e.event_type == EventType.WORKSPACE_CREATED for e in ws_events)


# ── 事件流可重放：最多一个终态 ──────────────────────────────────


async def test_terminal_events_at_most_one(sched):
    store, s = sched
    await _start_run(store)
    await s._fail("r1", "a")
    await s._fail("r1", "b")
    await s._complete("r1", "late")
    events = await store.get_events("r1")
    terminals = [e for e in events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)]
    assert len(terminals) == 1
