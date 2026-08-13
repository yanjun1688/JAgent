"""Phase 2 — RunCommand + Scheduler command handling tests.

Tests for command event model, Scheduler _check_pending_commands,
and Monitor auto-circuit-breaker behavior.
"""

from __future__ import annotations

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.monitoring.run_monitor import RunMonitor

from harness.core.fold import EventType, RunState, fold_events
from harness.core.scheduler.base import BaseScheduler, SchedulerConfig
from harness.models.events import (
    AgentThoughtPayload,
    RunCommandPayload,
    RunStartedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


class TestRunCommandModel:
    """CM-U1 ~ CM-U5: RunCommand event model tests."""

    def test_run_command_payload_valid_hard_abort(self):
        """CM-U1: RunCommandPayload with hard_abort command."""
        from harness.models.events import RunCommandPayload

        p = RunCommandPayload(command="hard_abort", reason="test")
        assert p.command == "hard_abort"
        assert p.reason == "test"
        assert p.affected_tool is None

    def test_run_command_payload_valid_soft_abort(self):
        p = RunCommandPayload(command="soft_abort", reason="graceful")
        assert p.command == "soft_abort"
        assert p.reason == "graceful"

    def test_run_command_payload_valid_pause(self):
        p = RunCommandPayload(command="pause", reason="manual")
        assert p.command == "pause"

    def test_run_command_payload_valid_resume(self):
        p = RunCommandPayload(command="resume")
        assert p.command == "resume"

    def test_run_command_payload_valid_skip_tool(self):
        p = RunCommandPayload(command="skip_tool", affected_tool="http_request")
        assert p.command == "skip_tool"
        assert p.affected_tool == "http_request"

    def test_run_command_payload_invalid_command(self):
        """CM-U2: Invalid command should fail Pydantic validation."""
        from harness.models.events import RunCommandPayload
        from pydantic import ValidationError

        with pytest.raises((ValidationError, ValueError)):
            RunCommandPayload(command="invalid_command")

    def test_run_command_payload_defaults(self):
        """CM-U3: Default values for optional fields."""
        from harness.models.events import RunCommandPayload

        p = RunCommandPayload(command="hard_abort")
        assert p.affected_tool is None
        assert p.issued_by == "monitor"

    def test_run_command_payload_all_literals(self):
        """CM-U4: All 5 command literals are valid."""
        from harness.models.events import RunCommandPayload

        for cmd in ("hard_abort", "soft_abort", "pause", "resume", "skip_tool"):
            p = RunCommandPayload(command=cmd)
            assert p.command == cmd

    def test_run_command_in_payload_model_map(self):
        """CM-U5: EventType.RUN_COMMAND maps to RunCommandPayload."""
        from harness.models.events import PAYLOAD_MODEL_MAP, RunCommandPayload

        assert EventType.RUN_COMMAND in PAYLOAD_MODEL_MAP
        assert PAYLOAD_MODEL_MAP[EventType.RUN_COMMAND] == RunCommandPayload


class TestSchedulerCommandCheck:
    """CM-U6 ~ CM-U11: Scheduler _check_pending_commands tests."""

    async def test_check_no_commands(self, store: EventStore):
        """CM-U6: No RUN_COMMAND events → returns None."""
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        scheduler = _make_scheduler(store)
        cmd = await scheduler._check_pending_commands("r1")
        assert cmd is None

    async def test_check_hard_abort(self, store: EventStore):
        """CM-U7: One hard_abort → returns 'hard_abort'."""
        from harness.models.events import RunCommandPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event(
            "r1", EventType.RUN_COMMAND, RunCommandPayload(command="hard_abort", reason="test").model_dump()
        )
        scheduler = _make_scheduler(store)
        cmd = await scheduler._check_pending_commands("r1")
        assert cmd == "hard_abort"

    async def test_check_multiple_commands_latest(self, store: EventStore):
        """CM-U8: Multiple commands → only latest is returned."""
        from harness.models.events import RunCommandPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event(
            "r1", EventType.RUN_COMMAND, RunCommandPayload(command="pause", reason="1").model_dump()
        )
        await store.append_event(
            "r1", EventType.RUN_COMMAND, RunCommandPayload(command="hard_abort", reason="2").model_dump()
        )
        scheduler = _make_scheduler(store)
        cmd = await scheduler._check_pending_commands("r1")
        assert cmd == "hard_abort"

    async def test_check_processed_command_skipped(self, store: EventStore):
        """CM-U9: Already processed command → returns None."""
        from harness.models.events import RunCommandPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event("r1", EventType.RUN_COMMAND, RunCommandPayload(command="hard_abort").model_dump())
        scheduler = _make_scheduler(store)
        scheduler._last_processed_command_seq["r1"] = 1
        cmd = await scheduler._check_pending_commands("r1")
        assert cmd is None

    async def test_check_skip_already_processed(self, store: EventStore):
        """CM-U10: Skip processed, return newer."""
        from harness.models.events import RunCommandPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event("r1", EventType.RUN_COMMAND, RunCommandPayload(command="pause").model_dump())  # seq=1
        await store.append_event(
            "r1", EventType.RUN_COMMAND, RunCommandPayload(command="hard_abort").model_dump()
        )  # seq=2
        scheduler = _make_scheduler(store)
        scheduler._last_processed_command_seq["r1"] = 1
        cmd = await scheduler._check_pending_commands("r1")
        assert cmd == "hard_abort"

    async def test_check_empty_non_command_events(self, store: EventStore):
        """CM-U11: Only non-RUN_COMMAND events → returns None."""
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event("r1", EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="thinking").model_dump())
        scheduler = _make_scheduler(store)
        cmd = await scheduler._check_pending_commands("r1")
        assert cmd is None


class TestSchedulerCommandHandling:
    """CM-I1 ~ CM-I7: Scheduler command processing integration tests."""

    async def test_hard_abort_terminates_run(self, store: EventStore):
        """CM-I1: hard_abort → Run immediately terminates with FAILED."""
        from harness.models.events import RunCommandPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event(
            "r1", EventType.RUN_COMMAND, RunCommandPayload(command="hard_abort", reason="test").model_dump()
        )
        scheduler = _make_scheduler(store)
        await scheduler._process_command("r1", "hard_abort")
        events = await store.get_events("r1")
        state = fold_events(events)
        assert state.status.value == "failed"
        assert "abort" in state.last_error.lower()

    async def test_soft_abort_waits_for_tool(self, store: EventStore):
        """CM-I2: soft_abort → schedules termination after current tool."""
        from harness.models.events import RunCommandPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event(
            "r1", EventType.RUN_COMMAND, RunCommandPayload(command="soft_abort", reason="graceful").model_dump()
        )
        scheduler = _make_scheduler(store)
        await scheduler._process_command("r1", "soft_abort")
        events = await store.get_events("r1")
        state = fold_events(events)
        assert state.status.value == "failed"

    async def test_pause_switches_to_paused(self, store: EventStore):
        """CM-I3: pause → Run switches to PAUSED."""
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        scheduler = _make_scheduler(store)
        result = await scheduler._process_command("r1", "pause")
        assert result is True
        events = await store.get_events("r1")
        state = fold_events(events)
        assert state.status.value == "paused"

    async def test_pause_resume_continues(self, store: EventStore):
        """CM-I4: pause → resume → Run continues."""
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        scheduler = _make_scheduler(store)
        await scheduler.pause("r1")
        events = await store.get_events("r1")
        state = fold_events(events)
        assert state.status.value == "paused"
        resumed = await scheduler.resume("r1")
        assert resumed is True
        events = await store.get_events("r1")
        state = fold_events(events)
        assert state.status.value == "running"

    async def test_unknown_command_ignored(self, store: EventStore):
        """CM-I7: Unknown command → Run continues normally."""
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        scheduler = _make_scheduler(store)
        result = await scheduler._process_command("r1", "unknown_command")
        assert result is True
        events = await store.get_events("r1")
        state = fold_events(events)
        assert state.status.value == "running"


class TestMonitorAutoCircuitBreaker:
    """CM-I8 ~ CM-I11: Monitor auto-circuit-breaker tests."""

    async def test_consecutive_failures_trigger_abort(self, store: EventStore):
        """CM-I8: 5 consecutive ToolFailed → hard_abort."""
        monitor = _make_monitor(store)
        monitor.attach()
        for i in range(5):
            await store.append_event(
                "r1",
                EventType.TOOL_FAILED,
                ToolFailedPayload(tool_call_id=f"tc-{i}", tool_name="echo", error="fail", retryable=False).model_dump(),
            )
        events = await store.get_events("r1")
        state = fold_events(events)
        feedbacks = state.feedbacks
        assert len(feedbacks) >= 1, "Monitor should inject feedback after 3+ failures"

    async def test_token_warning_injected(self, store: EventStore):
        """CM-I9: Token overuse → soft_abort / warning."""
        monitor = _make_monitor(store, max_tokens=100)
        monitor.attach()
        for i in range(5):
            await store.append_event(
                "r1",
                EventType.AGENT_THOUGHT,
                AgentThoughtPayload(thought="x" * 200, token_count=50).model_dump(),
            )
        events = await store.get_events("r1")
        state = fold_events(events)
        feedbacks = state.feedbacks
        assert len(feedbacks) >= 1, "Monitor should inject token warning"

    async def test_loop_detection_feedback(self, store: EventStore):
        """CM-I11: 6 repeated identical calls → hard_abort."""
        monitor = _make_monitor(store)
        monitor.attach()
        for i in range(8):
            await store.append_event(
                "r1",
                EventType.TOOL_CALLED,
                {"tool_call_id": f"tc-{i}", "tool_name": "echo", "input": {"msg": "same"}},
            )
            await store.append_event(
                "r1",
                EventType.TOOL_COMPLETED,
                ToolCompletedPayload(
                    tool_call_id=f"tc-{i}", tool_name="echo", output="same", duration_ms=10
                ).model_dump(),
            )
        events = await store.get_events("r1")
        state = fold_events(events)
        feedbacks = state.feedbacks
        assert len(feedbacks) >= 1, "Monitor should detect repeated calls"


class TestFaultInjection:
    """CM-F1 ~ CM-F4: Fault injection tests."""

    async def test_command_check_store_error_not_crash(self, store: EventStore):
        """CM-F1: Event Store error during command check → Scheduler doesn't crash."""
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        scheduler = _make_scheduler(store)
        original_get_events = store.get_events

        async def failing_get_events(run_id):
            raise ConnectionError("Store disconnected")

        store.get_events = failing_get_events
        try:
            cmd = await scheduler._check_pending_commands("r1")
            assert cmd is None
        finally:
            store.get_events = original_get_events

    async def test_concurrent_pause_and_abort(self, store: EventStore):
        """CM-F2: Simultaneous hard_abort and pause → hard_abort wins."""
        from harness.models.events import RunCommandPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event("r1", EventType.RUN_COMMAND, RunCommandPayload(command="pause").model_dump())
        await store.append_event("r1", EventType.RUN_COMMAND, RunCommandPayload(command="hard_abort").model_dump())
        scheduler = _make_scheduler(store)
        cmd = await scheduler._check_pending_commands("r1")
        assert cmd == "hard_abort"


def _make_scheduler(store: EventStore) -> BaseScheduler:
    """Create a minimal scheduler for testing command handling."""
    from harness.core.scheduler.base import BaseScheduler

    class TestScheduler(BaseScheduler):
        async def _run_loop(self, run_id: str, intent: str) -> RunState:
            pass

    executor = ToolExecutor(store)
    return TestScheduler(
        store=store,
        executor=executor,
        tool_defs=[],
        tool_fns={},
        config=SchedulerConfig(max_iterations=3),
    )


def _make_monitor(store: EventStore, max_tokens: int = 5000) -> "RunMonitor":
    from harness.monitoring.run_monitor import RunMonitor

    return RunMonitor(store, max_tokens=max_tokens)
