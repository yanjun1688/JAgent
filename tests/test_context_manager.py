"""Tests for V0.5 Context Manager — compression, checkpointing, resume, and scheduler integration."""

import pytest

from harness import (
    ContextManager,
    EventStore,
    EventType,
    MockAgentKernel,
    MockLLMClient,
    RetryPolicy,
    SideEffect,
    ThinkResult,
    ToolDefinition,
    ToolExecutor,
)
from harness.core.context_manager import ContextManager as ContextManagerCls
from harness.core.fold import RunState, fold_events
from harness.models.events import (
    ContextCheckpointedPayload,
    ContextCompressedPayload,
    EpisodeSummary,
)
from harness.core.scheduler import AgentLoopScheduler, SchedulerConfig


# ── Helpers ─────────────────────────────────────────────────────────


def _make_tool(name="test"):
    return ToolDefinition(
        name=name, description=name,
        input_schema={}, idempotency_key_fields=[],
        side_effects=[], retry_policy=RetryPolicy(max_retries=0),
    )


def _noop_fn(input):
    return {"result": "ok"}


async def _run_with_responses(store, responses, context_manager=None):
    """Helper: run a scheduler loop with pre-programmed MockAgentKernel responses."""
    tool_defs = [_make_tool("echo")]
    tool_fns = {"echo": _noop_fn}
    kernel = MockAgentKernel(responses)
    config = SchedulerConfig(max_iterations=50)
    executor = ToolExecutor(store)
    scheduler = AgentLoopScheduler(
        store, executor, kernel, tool_defs, tool_fns,
        config=config, context_manager=context_manager,
    )
    return await scheduler.run("run-1", "test")


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


# ── Token estimation ────────────────────────────────────────────────


class TestTokenEstimation:
    def test_estimate_empty_state(self):
        cm = ContextManagerCls(None)
        state = RunState(run_id="r")
        assert cm._estimate_context_tokens(state) == 1

    def test_estimate_with_content(self):
        cm = ContextManagerCls(None)
        state = RunState(run_id="r")
        from harness.core.fold import ToolResult, ToolResultStatus
        thought = type("obj", (), {"thought": "Hello world " * 10})()
        state.thought_history.append(thought)
        tr = ToolResult(
            tool_call_id="t1", tool_name="echo",
            status=ToolResultStatus.COMPLETED,
            output={"result": "done"},
        )
        state.tool_results.append(tr)
        estimate = cm._estimate_context_tokens(state)
        assert estimate > 0
        # ~140 chars × 0.25 = ~35 tokens
        assert estimate < 100

    def test_estimate_text_tokens(self):
        cm = ContextManagerCls(None)
        assert cm._estimate_text_tokens("Hello world!") > 0


# ── Compression trigger ──────────────────────────────────────────────


class TestCompressionTrigger:
    @pytest.mark.asyncio
    async def test_compression_triggers_when_over_threshold(self, store):
        cm = ContextManagerCls(store, token_limit=100, compression_threshold_ratio=0.5)
        # Build state with ~60+ token estimate (threshold=50)
        from harness.core.fold import ToolResult, ToolResultStatus
        state = RunState(run_id="r")
        for i in range(5):
            t = type("obj", (), {"thought": "x" * 50})()
            state.thought_history.append(t)

        await cm.maybe_compress("run-c1", 1, state)
        events = await store.get_events("run-c1")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        assert len(compressed) == 1
        p = ContextCompressedPayload.model_validate(compressed[0].payload)
        assert p.original_tokens > 0
        assert p.summary_ref.current_plan is not None

    @pytest.mark.asyncio
    async def test_compression_not_triggered_under_threshold(self, store):
        cm = ContextManagerCls(store, token_limit=128_000)
        state = RunState(run_id="r")
        t = type("obj", (), {"thought": "hello"})()
        state.thought_history.append(t)

        await cm.maybe_compress("run-c2", 1, state)
        events = await store.get_events("run-c2")
        assert not any(e.event_type == EventType.CONTEXT_COMPRESSED for e in events)

    @pytest.mark.asyncio
    async def test_compression_precision_high(self, store):
        cm = ContextManagerCls(store, token_limit=20, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        t = type("obj", (), {"thought": "x" * 50})()
        state.thought_history.append(t)

        await cm.maybe_compress("run-c3", 1, state)
        events = await store.get_events("run-c3")
        assert any(e.event_type == EventType.CONTEXT_COMPRESSED for e in events)

    @pytest.mark.asyncio
    async def test_summary_fallback_without_llm(self, store):
        cm = ContextManagerCls(store, token_limit=20, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        t = type("obj", (), {"thought": "x" * 50})()
        state.thought_history.append(t)

        await cm.maybe_compress("run-c4", 1, state)
        events = await store.get_events("run-c4")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        p = ContextCompressedPayload.model_validate(compressed[0].payload)
        assert isinstance(p.summary_ref, EpisodeSummary)
        assert p.summary_ref.current_plan is not None

    @pytest.mark.asyncio
    async def test_compression_cooldown_prevents_repeat(self, store):
        """Compression only fires once per checkpoint_interval iterations."""
        cm = ContextManagerCls(store, token_limit=20, compression_threshold_ratio=0.5,
                               checkpoint_interval=3)
        state = RunState(run_id="r")
        t = type("obj", (), {"thought": "x" * 100})()
        state.thought_history.append(t)

        # First call: should fire
        await cm.maybe_compress("run-cd1", 1, state)
        # Second call at same iteration: cooldown active, should NOT fire
        await cm.maybe_compress("run-cd1", 1, state)
        # Third call before cooldown expires: should NOT fire
        await cm.maybe_compress("run-cd1", 4, state)
        # Fourth call after cooldown: should fire again
        await cm.maybe_compress("run-cd1", 5, state)

        events = await store.get_events("run-cd1")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        assert len(compressed) == 2  # iterations 1 and 5

    @pytest.mark.asyncio
    async def test_compress_with_llm_client(self, store):
        mock_llm = MockLLMClient(["COMPRESSED SUMMARY: agent did X then Y"])
        cm = ContextManagerCls(
            store, llm_client=mock_llm,
            token_limit=100, compression_threshold_ratio=0.5,
        )
        from harness.core.fold import ToolResult, ToolResultStatus
        state = RunState(run_id="r")
        for _ in range(20):
            t = type("obj", (), {"thought": "x" * 30})()
            state.thought_history.append(t)
        tr = ToolResult(tool_call_id="t1", tool_name="echo",
                        status=ToolResultStatus.COMPLETED, output="done")
        state.tool_results.append(tr)

        await cm.maybe_compress("run-c5", 1, state)
        events = await store.get_events("run-c5")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        p = ContextCompressedPayload.model_validate(compressed[0].payload)
        assert "COMPRESSED SUMMARY" in p.summary_ref.current_plan
        assert mock_llm.calls  # LLM was actually called


# ── Checkpoint ──────────────────────────────────────────────────────


async def _state_from_store(store, run_id):
    """Helper: fold events from store into a RunState for try_checkpoint calls."""
    from harness.core.fold import fold_events
    events = await store.get_events(run_id)
    return fold_events(events) if events else RunState(run_id=run_id)


class TestCheckpoint:
    @pytest.mark.asyncio
    async def test_checkpoint_written_at_interval(self, store):
        cm = ContextManagerCls(store, checkpoint_interval=5)
        await store.append_event("run-chk1", EventType.RUN_STARTED,
                                 {"intent": "test", "context_snapshot": {}})
        for i in range(1, 8):
            await store.append_event("run-chk1", EventType.AGENT_THOUGHT,
                                     {"thought": f"t{i}", "token_count": 10})
            state = await _state_from_store(store, "run-chk1")
            await cm.try_checkpoint("run-chk1", i, state)

        events = await store.get_events("run-chk1")
        checkpoints = [e for e in events if e.event_type == EventType.CONTEXT_CHECKPOINTED]
        assert len(checkpoints) == 1  # only iteration 5
        p = ContextCheckpointedPayload.model_validate(checkpoints[0].payload)
        assert p.checkpoint_seq > 0
        assert "checkpoint_iter_5" in p.snapshot_ref

    @pytest.mark.asyncio
    async def test_checkpoint_not_written_on_interval_miss(self, store):
        cm = ContextManagerCls(store, checkpoint_interval=10)
        state = RunState(run_id="run-chk2")
        for i in range(1, 5):
            await cm.try_checkpoint("run-chk2", i, state)
        events = await store.get_events("run-chk2")
        assert not any(e.event_type == EventType.CONTEXT_CHECKPOINTED for e in events)

    @pytest.mark.asyncio
    async def test_checkpoint_writes_with_seq_zero(self, store):
        """Checkpoint writes even with seq=0 (no events yet but state provided)."""
        cm = ContextManagerCls(store, checkpoint_interval=1)
        state = RunState(run_id="run-chk3")
        await cm.try_checkpoint("run-chk3", 1, state)
        events = await store.get_events("run-chk3")
        checkpoints = [e for e in events if e.event_type == EventType.CONTEXT_CHECKPOINTED]
        assert len(checkpoints) == 1
        assert checkpoints[0].payload["checkpoint_seq"] == 0

    @pytest.mark.asyncio
    async def test_multiple_checkpoints(self, store):
        cm = ContextManagerCls(store, checkpoint_interval=3)
        await store.append_event("run-chk4", EventType.RUN_STARTED,
                                 {"intent": "test", "context_snapshot": {}})
        for i in range(1, 10):
            await store.append_event("run-chk4", EventType.AGENT_THOUGHT,
                                     {"thought": f"t{i}", "token_count": 10})
            state = await _state_from_store(store, "run-chk4")
            await cm.try_checkpoint("run-chk4", i, state)

        events = await store.get_events("run-chk4")
        checkpoints = [e for e in events if e.event_type == EventType.CONTEXT_CHECKPOINTED]
        assert len(checkpoints) == 3  # iterations 3, 6, 9


# ── Resume (find_resume_seq) ────────────────────────────────────────


class TestFindResumeSeq:
    def test_no_checkpoint_returns_zero(self, store):
        async def _test():
            await store.append_event("run-res1", EventType.RUN_STARTED,
                                     {"intent": "test", "context_snapshot": {}})
            events = await store.get_events("run-res1")
            assert ContextManagerCls.find_resume_seq(events) == 0
        import asyncio
        asyncio.run(_test())

    def test_with_checkpoints_returns_latest_seq(self, store):
        async def _test():
            await store.append_event("run-res2", EventType.RUN_STARTED,
                                     {"intent": "test", "context_snapshot": {}})
            await store.append_event("run-res2", EventType.CONTEXT_CHECKPOINTED,
                                     {"checkpoint_seq": 5, "snapshot_ref": "cp1", "token_count": 100})
            await store.append_event("run-res2", EventType.CONTEXT_CHECKPOINTED,
                                     {"checkpoint_seq": 12, "snapshot_ref": "cp2", "token_count": 200})
            events = await store.get_events("run-res2")
            assert ContextManagerCls.find_resume_seq(events) == 12
        import asyncio
        asyncio.run(_test())


# ── Scheduler integration ───────────────────────────────────────────


class TestSchedulerIntegration:
    @pytest.mark.asyncio
    async def test_context_manager_integrated_in_scheduler(self, store):
        """Context manager is called each iteration and writes checkpoints."""
        cm = ContextManagerCls(store, checkpoint_interval=3, token_limit=1_000_000)
        resp = [ThinkResult(thought=f"t{i}", tool_name="echo", tool_input={"x": i}) for i in range(7)]
        resp.append(ThinkResult(thought="done"))
        await _run_with_responses(store, resp, context_manager=cm)

        events = await store.get_events("run-1")
        checkpoints = [e for e in events if e.event_type == EventType.CONTEXT_CHECKPOINTED]
        assert len(checkpoints) >= 2  # iterations 3, 6

    @pytest.mark.asyncio
    async def test_compression_integrated_in_scheduler(self, store):
        """When token limit is low, ContextManager triggers compression during run."""
        cm = ContextManagerCls(store, token_limit=10, compression_threshold_ratio=0.5, checkpoint_interval=99)
        # Each thought has "t" + digit = ~2-3 chars → token estimate ~= 1
        # With 10 tiny thoughts, total chars ~30 → estimate ~= 7-8, near threshold
        resp = [ThinkResult(thought="x" * 20, tool_name="echo", tool_input={}) for _ in range(20)]
        resp.append(ThinkResult(thought="done"))
        await _run_with_responses(store, resp, context_manager=cm)

        events = await store.get_events("run-1")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        # With 20 thoughts each of length 20, char_count = 20*20=400, tokens ≈ 100
        # threshold = 5, so compression should trigger
        assert len(compressed) >= 1

    @pytest.mark.asyncio
    async def test_compression_does_not_break_run(self, store):
        """Compression events are written; run completes normally."""
        cm = ContextManagerCls(store, token_limit=5, compression_threshold_ratio=0.5, checkpoint_interval=2)
        resp = [ThinkResult(thought="x" * 10, tool_name="echo", tool_input={}) for _ in range(5)]
        resp.append(ThinkResult(thought="done"))
        result = await _run_with_responses(store, resp, context_manager=cm)

        assert result.status.value == "completed"
        events = await store.get_events("run-1")
        assert any(e.event_type == EventType.CONTEXT_COMPRESSED for e in events)
        assert any(e.event_type == EventType.CONTEXT_CHECKPOINTED for e in events)


# ── Long-running stress test (V0.5 acceptance) ──────────────────────


class TestLongRunning:
    @pytest.mark.asyncio
    async def test_100_iterations_without_overflow(self, store):
        """100+ tool call round trips: no overflow, clean completion."""
        cm = ContextManagerCls(store, token_limit=1000, checkpoint_interval=10)
        resp = [ThinkResult(thought=f"iteration {i}", tool_name="echo",
                            tool_input={"x": i}) for i in range(105)]
        resp.append(ThinkResult(thought="done"))
        kernel = MockAgentKernel(resp)
        config = SchedulerConfig(max_iterations=150)
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [_make_tool("echo")], {"echo": _noop_fn},
            config=config, context_manager=cm,
        )
        result = await scheduler.run("run-1", "test")

        assert result.status.value == "completed"
        events = await store.get_events("run-1")
        assert len(events) > 100

        checkpoints = [e for e in events if e.event_type == EventType.CONTEXT_CHECKPOINTED]
        assert len(checkpoints) >= 10

    @pytest.mark.asyncio
    async def test_100_iterations_with_compression(self, store):
        """Compression events are written during long runs."""
        cm = ContextManagerCls(store, token_limit=100, compression_threshold_ratio=0.5,
                               checkpoint_interval=10)
        resp = [ThinkResult(thought="Hello world " * 5, tool_name="echo",
                            tool_input={}) for _ in range(50)]
        resp.append(ThinkResult(thought="done"))
        kernel = MockAgentKernel(resp)
        config = SchedulerConfig(max_iterations=80)
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [_make_tool("echo")], {"echo": _noop_fn},
            config=config, context_manager=cm,
        )
        result = await scheduler.run("run-1", "test")

        assert result.status.value == "completed"
        events = await store.get_events("run-1")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        assert len(compressed) >= 1


# ── Structured summary (EpisodeSummary) ──────────────────────────────


class TestStructuredSummary:
    @pytest.mark.asyncio
    async def test_llm_returns_episode_summary(self, store):
        """When LLM returns valid JSON matching EpisodeSummary, summary_ref is EpisodeSummary."""
        import json
        mock_llm = MockLLMClient([json.dumps({
            "key_decisions": ["search for file", "parse output"],
            "tools_used": ["file_op", "grep"],
            "key_findings": ["found config file"],
            "errors_encountered": [],
            "current_plan": "Continue processing",
        })])
        cm = ContextManagerCls(
            store, llm_client=mock_llm,
            token_limit=100, compression_threshold_ratio=0.5,
        )
        from harness.models.events import EpisodeSummary
        state = RunState(run_id="r")
        for _ in range(20):
            t = type("obj", (), {"thought": "x" * 30})()
            state.thought_history.append(t)

        await cm.maybe_compress("run-ss1", 1, state)
        events = await store.get_events("run-ss1")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        p = ContextCompressedPayload.model_validate(compressed[0].payload)
        assert isinstance(p.summary_ref, EpisodeSummary)
        assert "search for file" in p.summary_ref.key_decisions
        assert "file_op" in p.summary_ref.tools_used
        assert "found config file" in p.summary_ref.key_findings
        assert p.summary_ref.current_plan == "Continue processing"
        assert p.summary_ref.errors_encountered == []

    @pytest.mark.asyncio
    async def test_llm_non_json_degrades_to_text(self, store):
        """When LLM returns non-JSON, content stored in current_plan field."""
        mock_llm = MockLLMClient(["Plain text summary of agent activity"])
        cm = ContextManagerCls(
            store, llm_client=mock_llm,
            token_limit=100, compression_threshold_ratio=0.5,
        )
        state = RunState(run_id="r")
        for _ in range(20):
            t = type("obj", (), {"thought": "x" * 30})()
            state.thought_history.append(t)

        await cm.maybe_compress("run-ss2", 1, state)
        events = await store.get_events("run-ss2")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        p = ContextCompressedPayload.model_validate(compressed[0].payload)
        assert isinstance(p.summary_ref, EpisodeSummary)
        assert "Plain text summary" in p.summary_ref.current_plan


# ── Emergency compression ────────────────────────────────────────────


class TestEmergencyCompression:
    def test_select_window_normal(self):
        """Normal compression (under emergency threshold) compresses all thoughts."""
        cm = ContextManagerCls(None, token_limit=1000,
                                compression_threshold_ratio=0.2)
        state = RunState(run_id="r")
        for _ in range(10):
            t = type("obj", (), {"thought": "x" * 100})()
            state.thought_history.append(t)

        window = cm.select_compression_window(state)
        assert window is not None
        assert len(window["compress_thoughts"]) == 10
        assert window["keep_count"] == 2

    def test_select_window_emergency(self):
        """Emergency compression (over emergency threshold) compresses oldest 50%."""
        cm = ContextManagerCls(None, token_limit=200,
                                compression_threshold_ratio=0.8,
                                emergency_threshold_ratio=0.9)
        state = RunState(run_id="r")
        for _ in range(10):
            t = type("obj", (), {"thought": "x" * 100})()
            state.thought_history.append(t)

        window = cm.select_compression_window(state)
        assert window is not None
        assert len(window["compress_thoughts"]) == 5  # oldest 50%
        assert window["keep_count"] == 3

    def test_select_window_below_threshold_returns_none(self):
        """When under compression threshold, returns None."""
        cm = ContextManagerCls(None, token_limit=1000,
                                compression_threshold_ratio=0.8)
        state = RunState(run_id="r")
        t = type("obj", (), {"thought": "hello"})()
        state.thought_history.append(t)

        window = cm.select_compression_window(state)
        assert window is None

    @pytest.mark.asyncio
    async def test_emergency_keep_count_propagated(self, store):
        """keep_recent_count=3 is written to event and folded into state."""
        cm = ContextManagerCls(store, token_limit=200,
                                compression_threshold_ratio=0.8,
                                emergency_threshold_ratio=0.9)
        state = RunState(run_id="r")
        for _ in range(10):
            t = type("obj", (), {"thought": "x" * 100})()
            state.thought_history.append(t)

        await cm.maybe_compress("run-em1", 1, state)
        events = await store.get_events("run-em1")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        p = ContextCompressedPayload.model_validate(compressed[0].payload)
        assert p.keep_recent_count == 3

        from harness.core.fold import fold_events
        folded = fold_events(events)
        assert folded.keep_recent_count == 3

    @pytest.mark.asyncio
    async def test_normal_keep_count_default(self, store):
        """Normal compression writes keep_recent_count=2."""
        cm = ContextManagerCls(store, token_limit=200,
                                compression_threshold_ratio=0.4)
        state = RunState(run_id="r")
        for _ in range(5):
            t = type("obj", (), {"thought": "x" * 100})()
            state.thought_history.append(t)

        await cm.maybe_compress("run-em2", 1, state)
        events = await store.get_events("run-em2")
        compressed = [e for e in events if e.event_type == EventType.CONTEXT_COMPRESSED]
        p = ContextCompressedPayload.model_validate(compressed[0].payload)
        assert p.keep_recent_count == 2


# ── LLMAgentKernel reads state.summary ─────────────────────────────


class TestKernelSummaryConsumption:
    @pytest.mark.asyncio
    async def test_kernel_uses_summary_when_present(self):
        """LLMAgentKernel should include summary in messages when state.summary is set."""
        from harness.core.agent_kernel import LLMAgentKernel
        from harness.core.fold import RunState

        mock_llm = MockLLMClient(["THOUGHT: using summary\n<STOP>"])
        kernel = LLMAgentKernel(mock_llm)
        state = RunState(run_id="r", summary="Agent did X then Y")
        td = _make_tool("echo")

        results = await kernel.think("test task", [td], state)
        assert results[0].thought == "using summary"

        # The system prompt should mention the summary
        last_call = mock_llm.calls[-1]
        msgs = last_call["messages"]
        summary_msgs = [m for m in msgs if m["role"] == "system" and "Previous context summary" in m["content"]]
        assert len(summary_msgs) == 1
        assert "Agent did X then Y" in summary_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_kernel_formats_episode_summary_fields(self):
        """LLMAgentKernel formats EpisodeSummary structured fields."""
        from harness.core.agent_kernel import LLMAgentKernel
        from harness.core.fold import RunState
        from harness.models.events import EpisodeSummary

        mock_llm = MockLLMClient(["THOUGHT: using structured summary\n<STOP>"])
        kernel = LLMAgentKernel(mock_llm)
        ep = EpisodeSummary(
            episode_range=(1, 20), original_tokens=500, compressed_tokens=50,
            key_decisions=["search file", "parse result"],
            tools_used=["file_op", "grep"],
            key_findings=["found config"],
            errors_encountered=["timeout on first try"],
            current_plan="continue analyzing",
            original_event_refs=[1, 2, 3, 4, 5],
        )
        state = RunState(run_id="r", summary=ep, keep_recent_count=3)
        td = _make_tool("echo")

        results = await kernel.think("test task", [td], state)
        assert results[0].thought == "using structured summary"

        last_call = mock_llm.calls[-1]
        msgs = last_call["messages"]
        summary_msgs = [m for m in msgs if m["role"] == "system" and "Previous context summary" in m["content"]]
        assert len(summary_msgs) == 1
        content = summary_msgs[0]["content"]
        assert "Key decisions: search file, parse result" in content
        assert "Tools used: file_op, grep" in content
        assert "Key findings: found config" in content
        assert "Errors: timeout on first try" in content
        assert "Current plan: continue analyzing" in content

    @pytest.mark.asyncio
    async def test_kernel_uses_keep_recent_count_for_window(self):
        """LLMAgentKernel uses keep_recent_count as window when summary present."""
        from harness.core.agent_kernel import LLMAgentKernel
        from harness.core.fold import RunState, ThoughtEntry

        mock_llm = MockLLMClient(["THOUGHT: doing work\nTOOL: echo\nARGS: {}\n<STOP>"])
        kernel = LLMAgentKernel(mock_llm)
        state = RunState(run_id="r", summary="test summary", keep_recent_count=3)
        for i in range(5):
            state.thought_history.append(ThoughtEntry(seq=i+1, thought=f"thought {i}"))
        td = _make_tool("echo")

        await kernel.think("test task", [td], state)
        last_call = mock_llm.calls[-1]
        msgs = last_call["messages"]
        thought_msgs = [m for m in msgs if m["role"] == "assistant" and "THOUGHT:" in m["content"]]
        assert len(thought_msgs) == 3  # window = max(keep_recent_count=3, 2) = 3
        assert "thought 4" in thought_msgs[-1]["content"]
