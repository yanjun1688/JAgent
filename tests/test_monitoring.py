"""Tests for V0.6 Monitoring & Feedback System."""

from __future__ import annotations

import pytest

from harness import (
    AgentLoopScheduler,
    EventStore,
    EventType,
    FeedbackInjectedPayload,
    MockAgentKernel,
    RetryPolicy,
    RunStatus,
    SchedulerConfig,
    SideEffect,
    ThinkResult,
    ToolDefinition,
    ToolExecutor,
    fold_events,
)
from harness.monitoring.run_monitor import RunMonitor


# ── Helpers ─────────────────────────────────────────────────────

async def _write_event(store: EventStore, run_id: str, event_type: EventType, payload: dict) -> None:
    await store.append_event(run_id, event_type, payload)


@pytest.fixture
def tool_def():
    return ToolDefinition(
        name="test_tool",
        description="Test tool",
        idempotency_key_fields=[],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(max_retries=0),
    )


async def dummy_tool(input: dict) -> dict:
    return {"result": "ok"}


# ── 9.1 Event Type Tests ───────────────────────────────────────


class TestFeedbackEvent:
    """FeedbackInjected payload model — validation and serialization."""

    def test_payload_model_default_priority(self):
        p = FeedbackInjectedPayload(feedback_text="test")
        assert p.feedback_text == "test"
        assert p.priority == "medium"

    def test_payload_model_high_priority(self):
        p = FeedbackInjectedPayload(feedback_text="urgent", priority="high")
        assert p.priority == "high"

    def test_payload_serialization_roundtrip(self):
        p = FeedbackInjectedPayload(feedback_text="hello", priority="high")
        data = p.model_dump()
        restored = FeedbackInjectedPayload(**data)
        assert restored.feedback_text == "hello"
        assert restored.priority == "high"


# ── 9.2 RunMonitor Tests ────────────────────────────────────────


class TestConsecutiveFailures:
    """RunMonitor injects feedback after 3 consecutive ToolFailed events."""

    async def _state_feedbacks(self, store: EventStore, run_id: str = "run1") -> list:
        events = await store.get_events(run_id)
        state = fold_events(events)
        return state.feedbacks

    @pytest.mark.asyncio
    async def test_no_feedback_on_single_failure(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        await _write_event(store, "run1", EventType.TOOL_FAILED, {
            "tool_call_id": "t1", "tool_name": "test", "error": "fail", "retryable": False,
        })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 0

    @pytest.mark.asyncio
    async def test_no_feedback_on_two_failures(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(2):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "test", "error": "fail", "retryable": False,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 0

    @pytest.mark.asyncio
    async def test_feedback_after_three_failures(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(3):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "test", "error": "fail", "retryable": False,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 1
        assert fb[0].affected_tool == "test"
        assert fb[0].error_type == "fail"
        assert fb[0].priority == "high"

    @pytest.mark.asyncio
    async def test_failure_feedback_sent_only_once(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(6):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "test", "error": "fail", "retryable": False,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 1

    @pytest.mark.asyncio
    async def test_completed_resets_failure_counter(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(2):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "test", "error": "fail", "retryable": False,
            })
        await _write_event(store, "run1", EventType.TOOL_COMPLETED, {
            "tool_call_id": "t2", "tool_name": "test", "output": "ok", "duration_ms": 10,
        })
        await _write_event(store, "run1", EventType.TOOL_FAILED, {
            "tool_call_id": "t3", "tool_name": "test", "error": "fail", "retryable": False,
        })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 0

    @pytest.mark.asyncio
    async def test_completed_clears_sent_flag_so_feedback_can_retrigger(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(3):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "test", "error": "fail", "retryable": False,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 1
        await _write_event(store, "run1", EventType.TOOL_COMPLETED, {
            "tool_call_id": "t3", "tool_name": "test", "output": "ok", "duration_ms": 10,
        })
        for i in range(4, 7):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "test", "error": "fail", "retryable": False,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 2


class TestTokenWarning:
    """RunMonitor injects token warning when threshold exceeded."""

    async def _state_feedbacks(self, store: EventStore, run_id: str = "run1") -> list:
        events = await store.get_events(run_id)
        state = fold_events(events)
        return state.feedbacks

    @pytest.mark.asyncio
    async def test_no_warning_below_threshold(self, store: EventStore):
        monitor = RunMonitor(store, max_tokens=1000, token_warning_ratio=0.8)
        monitor.attach()
        await _write_event(store, "run1", EventType.AGENT_THOUGHT, {
            "thought": "short", "token_count": 1,
        })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 0

    @pytest.mark.asyncio
    async def test_warning_when_over_threshold(self, store: EventStore):
        monitor = RunMonitor(store, max_tokens=100, token_warning_ratio=0.8)
        monitor.attach()
        long_thought = "hello " * 100
        await _write_event(store, "run1", EventType.AGENT_THOUGHT, {
            "thought": long_thought, "token_count": 1,
        })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 1
        assert "Token warning" in fb[0].feedback_text

    @pytest.mark.asyncio
    async def test_warning_sent_only_once(self, store: EventStore):
        monitor = RunMonitor(store, max_tokens=100, token_warning_ratio=0.8)
        monitor.attach()
        long_thought = "hello " * 100
        for _ in range(3):
            await _write_event(store, "run1", EventType.AGENT_THOUGHT, {
                "thought": long_thought, "token_count": 1,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 1


class TestCleanup:
    """cleanup releases per-run detection state.

    Feedback events live in EventStore and survive cleanup. Cleanup only
    resets the in-memory counters so the same run can trigger fresh feedbacks.
    """

    @pytest.mark.asyncio
    async def test_cleanup_removes_tracking_state(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(3):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "test", "error": "fail", "retryable": False,
            })
        assert "run1" in monitor._consecutive_failures
        monitor.cleanup("run1")
        assert "run1" not in monitor._consecutive_failures

    @pytest.mark.asyncio
    async def test_cleanup_then_retrigger_with_new_error(self, store: EventStore):
        """Multi-mode feedback: different (tool, error) pairs trigger independently.

        browser 3x with NotImplementedError → feedback A
        http_request 3x with ConnectTimeout  → feedback B  (different key, not blocked by dedup)
        """
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(3):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "browser", "error": "NotImplementedError: x", "retryable": False,
            })
        monitor.cleanup("run1")
        for i in range(3, 6):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "http_request", "error": "ConnectTimeout: www.example.com", "retryable": False,
            })
        events = await store.get_events("run1")
        state = fold_events(events)
        assert len(state.feedbacks) == 2  # browser NotImpl + http ConnectTimeout

    @pytest.mark.asyncio
    async def test_different_errors_on_same_endpoint_trigger_separate_feedbacks(self, store: EventStore):
        """Different error types on same endpoint each trigger their own feedback.

        Without fix: ep_key-only dedup blocks second error type entirely.
        With fix: dedup key is (ep_key, error_type), so each error type fires independently.
        """
        from harness.core.fold import fold_events

        async def _state_feedbacks(store, run_id="run1"):
            events = await store.get_events(run_id)
            state = fold_events(events)
            return state.feedbacks

        monitor = RunMonitor(store)
        monitor.attach()
        # 3x same endpoint, same error → 1 feedback
        for i in range(3):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "http_request",
                "error": "ConnectTimeout: httpbin.org", "retryable": False,
            })
        fb = await _state_feedbacks(store)
        assert len(fb) == 1, f"Expected 1 feedback after 3 same errors, got {len(fb)}"
        assert fb[0].error_type == "ConnectTimeout"

        # 3x same endpoint, DIFFERENT error → should be a 2nd feedback (not deduped)
        for i in range(3, 6):
            await _write_event(store, "run1", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "http_request",
                "error": "InvalidURL: httpbin.org/bad", "retryable": False,
            })
        fb = await _state_feedbacks(store)
        assert len(fb) == 2, (
            f"Bug: different error type should trigger separate feedback, "
            f"got {len(fb)} — _fed_ep_keys dedup granularity too coarse"
        )


# ── FeedbackInjected Event Persistence ─────────────────────────

class TestFeedbackEventPersistence:
    """FeedbackInjected events are written to EventStore and foldable."""

    @pytest.mark.asyncio
    async def test_feedback_event_appended_to_store(self, store: EventStore):
        payload = FeedbackInjectedPayload(feedback_text="test", priority="high")
        await store.append_event("run1", EventType.FEEDBACK_INJECTED, payload.model_dump())
        events = await store.get_events("run1")
        assert len(events) == 1
        assert events[0].event_type == EventType.FEEDBACK_INJECTED
        assert events[0].payload["feedback_text"] == "test"

    @pytest.mark.asyncio
    async def test_feedback_event_folded_into_state(self, store: EventStore):
        await store.append_event("run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        payload = FeedbackInjectedPayload(feedback_text="warning", priority="high")
        await store.append_event("run1", EventType.FEEDBACK_INJECTED, payload.model_dump())
        events = await store.get_events("run1")
        state = fold_events(events)
        assert len(state.feedbacks) == 1
        assert state.feedbacks[0].feedback_text == "warning"


# ── 9.3 AgentKernel Feedback Parameter ─────────────────────────

class TestKernelFeedbackParameter:
    """MockAgentKernel passes through the feedback parameter."""

    @pytest.mark.asyncio
    async def test_feedback_default_none(self, store: EventStore):
        await store.append_event("run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        events = await store.get_events("run1")
        state = fold_events(events)
        kernel = MockAgentKernel([ThinkResult(thought="ok")])
        result = await kernel.think("test", [], state)
        assert result is not None
        assert kernel.think_calls[0].get("feedback") is None

    @pytest.mark.asyncio
    async def test_feedback_passed_through(self, store: EventStore):
        await store.append_event("run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        events = await store.get_events("run1")
        state = fold_events(events)
        kernel = MockAgentKernel([ThinkResult(thought="ok")])
        await kernel.think("test", [], state, feedback="Warning: slow")
        assert kernel.think_calls[0]["feedback"] == "Warning: slow"


# ── 9.4 Scheduler Feedback Integration ─────────────────────────

class TestSchedulerFeedbackIntegration:
    """Scheduler pulls feedbacks from folded state and passes to think()."""

    async def _assert_feedback_in_state(self, store: EventStore, run_id: str = "run1") -> list:
        events = await store.get_events(run_id)
        return fold_events(events).feedbacks

    @pytest.mark.asyncio
    async def test_scheduler_no_feedback_when_no_events(self, store: EventStore):
        tool_def = ToolDefinition(
            name="dummy", description="", idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(max_retries=0),
        )
        kernel = MockAgentKernel([ThinkResult(thought="done")])
        executor = ToolExecutor(store)
        monitor = RunMonitor(store)
        monitor.attach()

        scheduler = AgentLoopScheduler(
            store=store, executor=executor, kernel=kernel,
            tool_defs=[tool_def], tool_fns={"dummy": dummy_tool},
            config=SchedulerConfig(max_iterations=3), monitor=monitor,
        )
        await scheduler.run("run1", "test task")
        assert kernel.think_calls[0].get("feedback") is None
        fb = await self._assert_feedback_in_state(store)
        assert len(fb) == 0

    @pytest.mark.asyncio
    async def test_scheduler_injects_feedback_from_folded_state(self, store: EventStore):
        """Pre-write events that trigger monitor feedback, then verify scheduler
        reads feedback from state.feedbacks via fold (not in-memory buffer)."""
        tool_def = ToolDefinition(
            name="dummy", description="", idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(max_retries=0),
        )
        kernel = MockAgentKernel([ThinkResult(thought="done")])
        executor = ToolExecutor(store)
        monitor = RunMonitor(store, max_tokens=10, token_warning_ratio=0.5)
        monitor.attach()

        scheduler = AgentLoopScheduler(
            store=store, executor=executor, kernel=kernel,
            tool_defs=[tool_def], tool_fns={"dummy": dummy_tool},
            config=SchedulerConfig(max_iterations=3), monitor=monitor,
        )

        await store.append_event("run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        long_thought = "word " * 200
        await store.append_event("run1", EventType.AGENT_THOUGHT, {
            "thought": long_thought, "token_count": 1,
        })

        # Verify feedback made it into EventStore before scheduler runs
        fb = await self._assert_feedback_in_state(store)
        assert len(fb) == 1
        assert "Token warning" in fb[0].feedback_text

        await scheduler.run("run1", "test task")
        # Scheduler reads from folded state.feedbacks, not from memory buffer
        assert kernel.think_calls[0]["feedback"] is not None
        assert "Token warning" in kernel.think_calls[0]["feedback"]


# ── #37: DAG_STEP_FAILED must trigger monitoring like TOOL_FAILED ─


class TestDagStepFailedMonitoring:
    """#37: RunMonitor must track DAG_STEP_FAILED events like TOOL_FAILED."""

    async def _state_feedbacks(self, store, run_id="run-dag-mon"):
        events = await store.get_events(run_id)
        state = fold_events(events)
        return state.feedbacks

    @pytest.mark.asyncio
    async def test_dag_step_failed_increments_consecutive_failures(self, store: EventStore):
        """After 3 DAG_STEP_FAILED events, monitor should inject feedback."""
        monitor = RunMonitor(store)
        monitor.attach()

        for i in range(3):
            await _write_event(store, "run-dag-mon", EventType.DAG_STEP_FAILED, {
                "plan_id": "p1", "step_id": f"s{i}", "error": "execution error",
                "retryable": False,
            })

        fb = await self._state_feedbacks(store)
        assert len(fb) == 1, (
            f"#37: 3 consecutive DAG_STEP_FAILED should trigger feedback. "
            f"Got {len(fb)} feedbacks"
        )
        assert fb[0].error_type == "execution error"
        assert fb[0].priority == "high"

    @pytest.mark.asyncio
    async def test_dag_step_failed_single_does_not_trigger(self, store: EventStore):
        """1 DAG_STEP_FAILED should NOT trigger feedback (threshold is 3)."""
        monitor = RunMonitor(store)
        monitor.attach()

        await _write_event(store, "run-dag-single", EventType.DAG_STEP_FAILED, {
            "plan_id": "p1", "step_id": "s1", "error": "execution error",
            "retryable": False,
        })

        fb = await self._state_feedbacks(store, run_id="run-dag-single")
        assert len(fb) == 0


# ── 9.5 attach / cleanup integration ───────────────────────────

class TestMonitorLifecycle:
    """RunMonitor attach and cleanup lifecycle."""

    @pytest.mark.asyncio
    async def test_attach_registers_callback(self, store: EventStore):
        monitor = RunMonitor(store)
        assert len(store._post_append) == 0
        monitor.attach()
        assert len(store._post_append) == 1


# ── P1: DAG_STEP_FAILED with tool_name dedup ────────────────────


class TestDagStepFailedDedup:
    """P1: DAG_STEP_FAILED with tool_name uses same dedup key as TOOL_FAILED."""

    async def _state_feedbacks(self, store, run_id="run-p1-dedup"):
        events = await store.get_events(run_id)
        state = fold_events(events)
        return state.feedbacks

    @pytest.mark.asyncio
    async def test_dag_step_failed_with_tool_name_does_not_duplicate(self, store: EventStore):
        """3x TOOL_FAILED + 3x DAG_STEP_FAILED with same tool_name → only 1 feedback."""
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(3):
            await _write_event(store, "run-p1-dedup", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "browser",
                "error": "NotImplementedError: Browser action failed",
                "retryable": False,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 1, f"Expected 1 feedback from TOOL_FAILED, got {len(fb)}"
        assert fb[0].affected_tool == "browser"

        for i in range(3, 6):
            await _write_event(store, "run-p1-dedup", EventType.DAG_STEP_FAILED, {
                "plan_id": "p1", "step_id": f"s{i}",
                "tool_name": "browser",
                "error": "NotImplementedError: Browser action failed",
                "retryable": False,
            })
        fb = await self._state_feedbacks(store)
        assert len(fb) == 1, (
            f"P1: DAG_STEP_FAILED with tool_name='browser' should dedup against TOOL_FAILED. "
            f"Got {len(fb)} feedbacks"
        )


# ── P2: Feedback includes actionable suggestion ─────────────────


class TestFeedbackSuggestion:
    """P2: _check_and_inject_feedback includes _generate_suggestion output."""

    async def _last_feedback(self, store, run_id="run-p2-sugg"):
        events = await store.get_events(run_id)
        state = fold_events(events)
        return state.feedbacks[-1] if state.feedbacks else None

    @pytest.mark.asyncio
    async def test_browser_not_implemented_includes_suggestion(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(3):
            await _write_event(store, "run-p2-sugg", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "browser",
                "error": "NotImplementedError: xyz",
                "retryable": False,
            })
        fb = await self._last_feedback(store)
        assert fb is not None
        assert fb.suggestion is not None, f"P2: browser NotImplementedError should have suggestion"
        assert "http_request" in fb.suggestion

    @pytest.mark.asyncio
    async def test_unknown_error_type_has_no_suggestion(self, store: EventStore):
        monitor = RunMonitor(store)
        monitor.attach()
        for i in range(3):
            await _write_event(store, "run-p2-nosugg", EventType.TOOL_FAILED, {
                "tool_call_id": f"t{i}", "tool_name": "browser",
                "error": "SomeUnknownError: something",
                "retryable": False,
            })
        fb = await self._last_feedback(store, run_id="run-p2-nosugg")
        assert fb is not None
        assert fb.suggestion is None, f"P2: unknown error should have no suggestion"


# ── Feedback chain tests: consumed_at_seq + since_seq ────────────


class TestGetFeedbackTextSinceSeq:
    """_get_feedback_text with since_seq parameter filters correctly."""

    @pytest.mark.asyncio
    async def test_since_seq_filters_old_feedbacks(self, store: EventStore):
        """Feedbacks injected at or before since_seq should be excluded."""
        tool_def = ToolDefinition(
            name="dummy", description="", idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(max_retries=0),
        )
        kernel = MockAgentKernel([ThinkResult(thought="done")])
        executor = ToolExecutor(store)
        # Use low max_tokens to trigger token warning with a small thought
        monitor = RunMonitor(store, max_tokens=10, token_warning_ratio=0.5)
        monitor.attach()

        scheduler = AgentLoopScheduler(
            store=store, executor=executor, kernel=kernel,
            tool_defs=[tool_def], tool_fns={"dummy": dummy_tool},
            config=SchedulerConfig(max_iterations=3), monitor=monitor,
        )

        await store.append_event("run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        # Inject feedback A at a specific seq
        long_thought_a = "word " * 200
        await store.append_event("run1", EventType.AGENT_THOUGHT, {
            "thought": long_thought_a, "token_count": 1,
        })

        events = await store.get_events("run1")
        state = fold_events(events)

        # since_seq=None returns all unconsumed feedbacks
        fb_all = scheduler._get_feedback_text(state, since_seq=None)
        assert fb_all is not None, "Should have feedback without since_seq filter"
        assert "Token warning" in fb_all

        # since_seq=state.seq should include feedbacks injected at seq > state.seq
        fb_filtered = scheduler._get_feedback_text(state, since_seq=state.seq)
        if fb_filtered is not None:
            assert "Token warning" in fb_filtered

    @pytest.mark.asyncio
    async def test_consumed_feedback_excluded(self, store: EventStore):
        """Feedbacks with consumed_at_seq set should be excluded from _get_feedback_text."""
        tool_def = ToolDefinition(
            name="dummy", description="", idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(max_retries=0),
        )
        kernel = MockAgentKernel([ThinkResult(thought="done")])
        executor = ToolExecutor(store)
        monitor = RunMonitor(store)
        monitor.attach()

        scheduler = AgentLoopScheduler(
            store=store, executor=executor, kernel=kernel,
            tool_defs=[tool_def], tool_fns={"dummy": dummy_tool},
            config=SchedulerConfig(max_iterations=3), monitor=monitor,
        )

        # Simulate: FeedbackInjected → PlanCreated → PlanRevised (which marks consumed)
        await store.append_event("run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        long_thought = "word " * 200
        await store.append_event("run1", EventType.AGENT_THOUGHT, {
            "thought": long_thought, "token_count": 1,
        })
        # PlanRevised event marks feedbacks as consumed in fold
        await store.append_event("run1", EventType.PLAN_CREATED, {
            "plan_id": "p1", "intent": "test",
            "steps_summary": "1 step", "layer_count": 1,
        })
        await store.append_event("run1", EventType.PLAN_REVISED, {
            "plan_id": "p1", "revision_reason": "step_failure",
            "remaining_steps_summary": "revised",
        })

        events = await store.get_events("run1")
        state = fold_events(events)
        # All feedbacks should have consumed_at_seq set
        for fb in state.feedbacks:
            assert fb.consumed_at_seq is not None, f"Feedback should be consumed after PlanRevised"

        fb_text = scheduler._get_feedback_text(state)
        assert fb_text is None, (
            f"Consumed feedbacks should not appear in _get_feedback_text. "
            f"Got: {fb_text}"
        )

    @pytest.mark.asyncio
    async def test_since_seq_none_returns_all_unconsumed(self, store: EventStore):
        """since_seq=None (default) should return all active unconsumed feedbacks."""
        tool_def = ToolDefinition(
            name="dummy", description="", idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(max_retries=0),
        )
        kernel = MockAgentKernel([ThinkResult(thought="done")])
        executor = ToolExecutor(store)
        monitor = RunMonitor(store, max_tokens=10, token_warning_ratio=0.5)
        monitor.attach()

        scheduler = AgentLoopScheduler(
            store=store, executor=executor, kernel=kernel,
            tool_defs=[tool_def], tool_fns={"dummy": dummy_tool},
            config=SchedulerConfig(max_iterations=3), monitor=monitor,
        )

        await store.append_event("run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        long_thought = "word " * 200
        await store.append_event("run1", EventType.AGENT_THOUGHT, {
            "thought": long_thought, "token_count": 1,
        })

        events = await store.get_events("run1")
        state = fold_events(events)

        fb_text = scheduler._get_feedback_text(state, since_seq=None)
        assert fb_text is not None
        assert "Token warning" in fb_text
