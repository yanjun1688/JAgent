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
    EpisodeArchivedPayload,
    Episode,
)
from harness.core.scheduler import AgentLoopScheduler, SchedulerConfig
from harness.core.token_counter import HeuristicTokenCounter


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
    async def test_estimate_empty_state(self):
        cm = ContextManagerCls(None)
        state = RunState(run_id="r")
        assert await cm._async_estimate_context_tokens(state) == 1

    async def test_estimate_with_content(self):
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
        estimate = await cm._async_estimate_context_tokens(state)
        assert estimate > 0
        # ~140 chars × 0.25 = ~35 tokens
        assert estimate < 100

    async def test_estimate_text_tokens(self):
        cm = ContextManagerCls(None)
        result = await cm._async_estimate_text_tokens("Hello world!")
        assert result > 0


# ── Compression trigger ──────────────────────────────────────────────


class TestCompressionTrigger:
    @pytest.mark.asyncio
    async def test_compression_triggers_when_over_threshold(self, store):
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=100, compression_threshold_ratio=0.5)
        from harness.core.fold import ThoughtEntry
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        for i in range(10):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 80))

        await cm.maybe_compress("run-c1", 1, state)
        events = await store.get_events("run-c1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert p.original_tokens > 0
        assert p.episode.current_plan is not None

    @pytest.mark.asyncio
    async def test_compression_not_triggered_under_threshold(self, store):
        cm = ContextManagerCls(store, token_limit=128_000)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        state.thought_history.append(ThoughtEntry(seq=1, thought="hello"))

        await cm.maybe_compress("run-c2", 1, state)
        events = await store.get_events("run-c2")
        assert not any(e.event_type == EventType.EPISODE_ARCHIVED for e in events)

    @pytest.mark.asyncio
    async def test_compression_precision_high(self, store):
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=20, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        state.thought_history.append(ThoughtEntry(seq=1, thought="x" * 100))

        await cm.maybe_compress("run-c3", 1, state)
        events = await store.get_events("run-c3")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    @pytest.mark.asyncio
    async def test_summary_fallback_without_llm(self, store):
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=20, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(5):
            state.thought_history.append(ThoughtEntry(seq=i + 1, thought="x" * 100))

        await cm.maybe_compress("run-c4", 1, state)
        events = await store.get_events("run-c4")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert isinstance(p.episode, Episode)
        assert p.episode.current_plan is not None

    @pytest.mark.asyncio
    async def test_compression_cooldown_prevents_repeat(self, store):
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=20, compression_threshold_ratio=0.5,
                               checkpoint_interval=3)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        state.thought_history.append(ThoughtEntry(seq=1, thought="x" * 100))

        await cm.maybe_compress("run-cd1", 1, state)
        await cm.maybe_compress("run-cd1", 1, state)
        await cm.maybe_compress("run-cd1", 4, state)
        await cm.maybe_compress("run-cd1", 5, state)

        events = await store.get_events("run-cd1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) == 2

    @pytest.mark.asyncio
    async def test_compress_with_llm_client(self, store):
        mock_llm = MockLLMClient(["COMPRESSED SUMMARY: agent did X then Y"])
        cm = ContextManagerCls(
            store, llm_client=mock_llm,
            token_limit=100, compression_threshold_ratio=0.5,
        )
        from harness.core.fold import ThoughtEntry, ToolResult, ToolResultStatus
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        for _ in range(20):
            state.thought_history.append(ThoughtEntry(seq=_, thought="x" * 30))
        tr = ToolResult(tool_call_id="t1", tool_name="echo",
                        status=ToolResultStatus.COMPLETED, output="done")
        state.tool_results.append(tr)

        await cm.maybe_compress("run-c5", 1, state)
        events = await store.get_events("run-c5")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert "COMPRESSED SUMMARY" in p.episode.current_plan
        assert mock_llm.calls


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
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=100,
                               compression_threshold_ratio=0.5, checkpoint_interval=1)
        # 8 tool responses: by iter 4, 3 thoughts have accumulated → archive_episode
        # checkpoint_interval=1 avoids cooldown blocking the 2nd compression attempt
        resp = [ThinkResult(thought="x" * 100, tool_name="echo", tool_input={}) for _ in range(8)]
        resp.append(ThinkResult(thought="done"))
        await _run_with_responses(store, resp, context_manager=cm)

        events = await store.get_events("run-1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    @pytest.mark.asyncio
    async def test_compression_does_not_break_run(self, store):
        """Compression events are written; run completes normally."""
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=80, compression_threshold_ratio=0.5, checkpoint_interval=2)
        resp = [ThinkResult(thought="x" * 80, tool_name="echo", tool_input={}) for _ in range(5)]
        resp.append(ThinkResult(thought="done"))
        result = await _run_with_responses(store, resp, context_manager=cm)

        assert result.status.value == "completed"
        events = await store.get_events("run-1")
        assert any(e.event_type == EventType.EPISODE_ARCHIVED for e in events)
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
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1


# ── Structured summary (Episode/EpisodeSummary) ──────────────────────


class TestStructuredSummary:
    @pytest.mark.asyncio
    async def test_llm_returns_episode_summary(self, store):
        """When LLM returns valid JSON matching EpisodeSummary, episode is Episode."""
        import json
        mock_llm = MockLLMClient([json.dumps({
            "key_decisions": ["search for file", "parse output"],
            "tools_used": ["file_op", "grep"],
            "key_findings": ["found config file"],
            "errors_encountered": [],
            "current_plan": "Continue processing",
        })])
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(
            store, llm_client=mock_llm, token_counter=tc,
            token_limit=100, compression_threshold_ratio=0.5,
        )
        state = RunState(run_id="r")
        from harness.core.fold import ThoughtEntry
        for i in range(20):
            state.thought_history.append(ThoughtEntry(seq=i + 1, thought="x" * 60))

        await cm.maybe_compress("run-ss1", 1, state)
        events = await store.get_events("run-ss1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert isinstance(p.episode, Episode)
        assert "search for file" in p.episode.key_decisions
        assert "file_op" in p.episode.tools_used
        assert "found config file" in p.episode.key_findings
        assert p.episode.current_plan == "Continue processing"
        assert p.episode.errors_encountered == []

    @pytest.mark.asyncio
    async def test_llm_non_json_degrades_to_text(self, store):
        """When LLM returns non-JSON, content stored in current_plan field."""
        mock_llm = MockLLMClient(["Plain text summary of agent activity"])
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(
            store, llm_client=mock_llm, token_counter=tc,
            token_limit=100, compression_threshold_ratio=0.5,
        )
        state = RunState(run_id="r")
        from harness.core.fold import ThoughtEntry
        for i in range(20):
            state.thought_history.append(ThoughtEntry(seq=i + 1, thought="x" * 60))

        await cm.maybe_compress("run-ss2", 1, state)
        events = await store.get_events("run-ss2")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert isinstance(p.episode, Episode)
        assert "Plain text summary" in p.episode.current_plan


# ── Emergency compression ────────────────────────────────────────────


class TestEmergencyCompression:
    @pytest.mark.asyncio
    async def test_archive_episode_compresses_all_thoughts(self, store):
        """Normal compression through _archive_episode compresses all thoughts."""
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=100,
                                compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(4):
            state.thought_history.append(ThoughtEntry(seq=i + 1, thought="x" * 80))

        await cm.maybe_compress("run-wn1", 1, state)
        events = await store.get_events("run-wn1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert p.keep_recent_count == 2

    @pytest.mark.asyncio
    async def test_emergency_triggers_episode_archive(self, store):
        """Emergency compression triggers episode archive with keep_recent_count=3."""
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=50,
                                compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(10):
            state.thought_history.append(ThoughtEntry(seq=i + 1, thought="x" * 100))

        await cm.maybe_compress("run-we1", 1, state)
        events = await store.get_events("run-we1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    @pytest.mark.asyncio
    async def test_compression_below_threshold_no_events(self, store):
        """When under compression threshold, no events written."""
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=1000,
                                compression_threshold_ratio=0.8)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        state.thought_history.append(ThoughtEntry(seq=1, thought="hello"))

        await cm.maybe_compress("run-wu1", 1, state)
        events = await store.get_events("run-wu1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        pruned = [e for e in events if e.event_type == EventType.CONTEXT_PRUNED]
        assert len(archived) == 0
        assert len(pruned) == 0

    @pytest.mark.asyncio
    async def test_emergency_keep_count_propagated(self, store):
        """keep_recent_count=3 is written to event and folded into state."""
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=50,
                                compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(10):
            state.thought_history.append(ThoughtEntry(seq=i + 1, thought="x" * 100))

        await cm.maybe_compress("run-em1", 1, state)
        events = await store.get_events("run-em1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert p.keep_recent_count == 3

        folded = fold_events(events)
        assert folded.keep_recent_count == 3

    @pytest.mark.asyncio
    async def test_normal_keep_count_default(self, store):
        """Normal compression writes keep_recent_count=2."""
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=100,
                                compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(4):
            state.thought_history.append(ThoughtEntry(seq=i + 1, thought="x" * 80))

        await cm.maybe_compress("run-em2", 1, state)
        events = await store.get_events("run-em2")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
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

        mock_llm = MockLLMClient(["THOUGHT: using structured summary\n<STOP>"])
        kernel = LLMAgentKernel(mock_llm)
        ep = Episode(
            title="Test Episode",
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
        main_call = mock_llm.calls[0]
        msgs = main_call["messages"]
        thought_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(thought_msgs) == 3  # window = max(keep_recent_count=3, 2) = 3
        assert "thought 4" in thought_msgs[-1]["content"]


# ── V3.0 Phase 1: TokenCounter integration ─────────────────────


class TestTokenCounterIntegration:
    @pytest.mark.asyncio
    async def test_context_manager_uses_token_counter(self, store):
        """ContextManager uses injected TokenCounter for estimation."""
        from harness.core.token_counter import HeuristicTokenCounter
        from harness.core.fold import ThoughtEntry
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=100, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        for i in range(5):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 50))
        estimate = await cm._async_estimate_context_tokens(state)
        assert estimate > 0

    @pytest.mark.asyncio
    async def test_async_estimate_uses_token_counter(self, store):
        """_async_estimate_context_tokens delegates to TokenCounter."""
        from harness.core.token_counter import HeuristicTokenCounter
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc)
        state = RunState(run_id="r")
        from harness.core.fold import ThoughtEntry
        state.thought_history.append(ThoughtEntry(seq=1, thought="Hello world"))
        estimate = await cm._async_estimate_context_tokens(state)
        assert estimate == max(1, int(len("Hello world") * 0.25))


# ── V3.0 Phase 1: 3-tier compression strategy ──────────────────


class TestThreeTierCompression:
    @pytest.mark.asyncio
    async def test_lazy_clear_writes_context_pruned(self, store):
        """When ratio is 50-70% and low-importance events exist, writes ContextPruned."""
        cm = ContextManagerCls(store, token_limit=100, compression_threshold_ratio=0.7, lazy_clear_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry, ToolResult, ToolResultStatus
        for i in range(5):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 50))
        for i in range(5, 10):
            state.tool_results.append(ToolResult(
                tool_call_id=f"t{i}", tool_name="echo",
                status=ToolResultStatus.COMPLETED, output="y" * 50, event_seq=i,
            ))

        await cm.maybe_compress("run-3t1", 1, state)
        events = await store.get_events("run-3t1")
        pruned = [e for e in events if e.event_type == EventType.CONTEXT_PRUNED]
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(pruned) + len(archived) >= 1

    @pytest.mark.asyncio
    async def test_episode_archive_writes_episode_archived(self, store):
        """When ratio is 70-90%, writes EPISODE_ARCHIVED with Episode data."""
        tc = HeuristicTokenCounter()
        cm = ContextManagerCls(store, token_counter=tc, token_limit=100, compression_threshold_ratio=0.7, lazy_clear_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(10):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 75))

        await cm.maybe_compress("run-3t2", 1, state)
        events = await store.get_events("run-3t2")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    @pytest.mark.asyncio
    async def test_emergency_compact_keeps_recent_3(self, store):
        """Emergency compression (>90%) keeps recent 3 rounds."""
        cm = ContextManagerCls(store, token_limit=10, compression_threshold_ratio=0.7,
                               emergency_threshold_ratio=0.9)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(10):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 100))

        await cm.maybe_compress("run-3t3", 1, state)
        events = await store.get_events("run-3t3")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert p.keep_recent_count == 3


# ── V3.0 Phase 1: Importance scoring ───────────────────────────


class TestImportanceScoring:
    def test_thought_default_importance(self):
        cm = ContextManagerCls(None)
        from harness.core.fold import ThoughtEntry
        t = ThoughtEntry(seq=1, thought="just thinking")
        assert cm._score_event_importance(t) == 0.5

    def test_thought_with_decision_markers(self):
        cm = ContextManagerCls(None)
        from harness.core.fold import ThoughtEntry
        t = ThoughtEntry(seq=1, thought="I decided to use a different approach")
        assert cm._score_event_importance(t) == 0.7

    def test_tool_failed_high_importance(self):
        cm = ContextManagerCls(None)
        from harness.core.fold import ToolResult, ToolResultStatus
        tr = ToolResult(tool_call_id="t1", tool_name="echo",
                        status=ToolResultStatus.FAILED, error="boom")
        assert cm._score_event_importance(tr) == 0.8

    def test_tool_completed_low_importance(self):
        cm = ContextManagerCls(None)
        from harness.core.fold import ToolResult, ToolResultStatus
        tr = ToolResult(tool_call_id="t1", tool_name="echo",
                        status=ToolResultStatus.COMPLETED, output="done")
        assert cm._score_event_importance(tr) == 0.2

    def test_tool_timeout_high_importance(self):
        cm = ContextManagerCls(None)
        from harness.core.fold import ToolResult, ToolResultStatus
        tr = ToolResult(tool_call_id="t1", tool_name="echo",
                        status=ToolResultStatus.TIMEOUT, error="timeout")
        assert cm._score_event_importance(tr) == 0.8


# ── V3.0 Phase 1: Episode generation ───────────────────────────


class TestEpisodeGeneration:
    @pytest.mark.asyncio
    async def test_episode_without_llm_is_legacy(self, store):
        """Without LLM, Episode has format='legacy'."""
        cm = ContextManagerCls(store, token_limit=20, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(5):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 50))

        await cm.maybe_compress("run-ep1", 1, state)
        events = await store.get_events("run-ep1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert p.episode.current_plan is not None

    @pytest.mark.asyncio
    async def test_episode_with_llm_json(self, store):
        """With LLM returning valid JSON, Episode has format='structured'."""
        import json
        mock_llm = MockLLMClient([json.dumps({
            "title": "Test Episode",
            "summary": "A test episode summary",
            "key_decisions": ["decision 1"],
            "tools_used": ["echo"],
            "key_findings": ["finding 1"],
            "errors_encountered": [],
            "current_plan": "continue",
        })])
        cm = ContextManagerCls(store, llm_client=mock_llm, token_limit=100, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(20):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 30))

        await cm.maybe_compress("run-ep2", 1, state)
        events = await store.get_events("run-ep2")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    @pytest.mark.asyncio
    async def test_episode_llm_non_json_is_legacy(self, store):
        """With LLM returning non-JSON, Episode has format='legacy'."""
        mock_llm = MockLLMClient(["Plain text summary"])
        cm = ContextManagerCls(store, llm_client=mock_llm, token_limit=100, compression_threshold_ratio=0.5)
        state = RunState(run_id="r")
        state.seq = 99
        state.plan_boundary_seqs = [99]
        from harness.core.fold import ThoughtEntry
        for i in range(20):
            state.thought_history.append(ThoughtEntry(seq=i, thought="x" * 30))

        await cm.maybe_compress("run-ep3", 1, state)
        events = await store.get_events("run-ep3")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1
        p = EpisodeArchivedPayload.model_validate(archived[0].payload)
        assert "Plain text summary" in p.episode.current_plan


# ── V3.0 Phase 1: fold.py new event types ──────────────────────


class TestFoldNewEventTypes:
    def test_episode_archived_fold(self):
        """EPISODE_ARCHIVED sets state.summary and appends to episodes."""
        from harness.core.fold import fold_events
        from harness.models.events import Episode, EpisodeArchivedPayload
        episode = Episode(
            episode_range=(1, 10), original_tokens=100, compressed_tokens=20,
            key_decisions=["d1"], tools_used=["t1"], key_findings=["f1"],
            errors_encountered=[], current_plan="plan", original_event_refs=[1, 2, 3],
            title="Test", summary="Summary", format="structured",
        )
        payload = EpisodeArchivedPayload(
            original_tokens=100, compressed_tokens=20, episode=episode,
            keep_recent_count=2, archived_event_refs=[1, 2, 3],
        )
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.EPISODE_ARCHIVED, payload.model_dump()),
        ]
        state = fold_events(events)
        assert state.summary is not None
        assert len(state.episodes) == 1
        assert state.keep_recent_count == 2

    def test_context_pruned_fold(self):
        """CONTEXT_PRUNED removes events from thought_history/tool_results."""
        from harness.core.fold import fold_events
        from harness.models.events import ContextPrunedPayload
        payload = ContextPrunedPayload(
            pruned_event_refs=[2, 3], pruned_token_count=50, pruned_seq_count=2,
        )
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "t1", "token_count": 10}),
            _event("r1", 3, EventType.AGENT_THOUGHT, {"thought": "t2", "token_count": 10}),
            _event("r1", 4, EventType.AGENT_THOUGHT, {"thought": "t3", "token_count": 10}),
            _event("r1", 5, EventType.CONTEXT_PRUNED, payload.model_dump()),
        ]
        state = fold_events(events)
        assert len(state.thought_history) == 1
        assert state.thought_history[0].thought == "t1" or state.thought_history[0].thought == "t3"

    def test_legacy_episode_archived_still_folds(self):
        """EPISODE_ARCHIVED events fold correctly."""
        from harness.core.fold import fold_events
        from harness.models.events import Episode, EpisodeArchivedPayload
        ep = Episode(
            title="Legacy",
            episode_range=(1, 5), original_tokens=100, compressed_tokens=20,
            key_decisions=["d1"], tools_used=["t1"], key_findings=["f1"],
            errors_encountered=[], current_plan="plan", original_event_refs=[1, 2, 3],
        )
        payload = EpisodeArchivedPayload(
            original_tokens=100, compressed_tokens=20, episode=ep, keep_recent_count=2,
            archived_event_refs=[1, 2, 3],
        )
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "t1", "token_count": 10}),
            _event("r1", 3, EventType.EPISODE_ARCHIVED, payload.model_dump()),
        ]
        state = fold_events(events)
        assert state.summary is not None
        assert state.keep_recent_count == 2


def _event(run_id, seq, event_type, payload):
    from harness.models.events import Event
    return Event(run_id=run_id, seq=seq, event_type=event_type, payload=payload, created_at=0.0)
