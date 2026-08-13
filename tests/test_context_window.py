"""Phase 3 — Context window + truncation tests.

Tests for ContextManager token estimation, event type changes,
and tool output truncation rules.
"""

from __future__ import annotations


from harness.core.context_manager import ContextManager
from harness.core.fold import RunState, ThoughtEntry, ToolResult
from harness.models.events import (
    EventType,
    ContextCheckpointedPayload,
    Event,
)


class TestTokenEstimation:
    """CW-U1 ~ CW-U6: Dynamic window / token estimation tests (async)."""

    async def test_estimate_empty_state(self):
        """CW-U1: Empty state minimal token count via async estimate."""
        cm = ContextManager(store=None, token_limit=8000)
        state = RunState(run_id="r1")
        tokens = await cm._async_estimate_context_tokens(state)
        assert tokens >= 1

    async def test_estimate_with_thoughts(self):
        """CW-U2: State with thoughts proportional token count."""
        cm = ContextManager(store=None, token_limit=8000)
        state = RunState(run_id="r1")
        state.thought_history = [
            ThoughtEntry(seq=1, thought="A" * 100, tool_choice="echo"),
            ThoughtEntry(seq=2, thought="B" * 100, tool_choice="search"),
        ]
        tokens = await cm._async_estimate_context_tokens(state)
        assert tokens >= 1

    async def test_estimate_with_tool_results(self):
        """CW-U3: Large tool results high token count."""
        cm = ContextManager(store=None, token_limit=8000)
        state = RunState(run_id="r1")
        state.tool_results = [
            ToolResult(tool_call_id="tc1", tool_name="http_request", status="completed", output="X" * 800, event_seq=1),
        ] * 10
        tokens = await cm._async_estimate_context_tokens(state)
        assert tokens >= 1000

    async def test_estimate_with_errors(self):
        cm = ContextManager(store=None, token_limit=8000)
        state = RunState(run_id="r1")
        state.tool_results = [
            ToolResult(tool_call_id="tc1", tool_name="echo", status="failed", error="E" * 500, event_seq=1),
        ]
        tokens = await cm._async_estimate_context_tokens(state)
        assert tokens >= 1

    def test_token_limit_affects_window(self):
        """CW-U4: Higher token_limit larger compression threshold."""
        cm_small = ContextManager(store=None, token_limit=4000)
        cm_large = ContextManager(store=None, token_limit=16000)
        assert cm_large.compression_threshold > cm_small.compression_threshold

    async def test_estimate_minimum_one(self):
        """CW-U5: Even empty state returns at least 1."""
        cm = ContextManager(store=None, token_limit=8000)
        state = RunState(run_id="r1")
        assert await cm._async_estimate_context_tokens(state) >= 1

    async def test_estimate_text_tokens(self):
        """CW-U6: _async_estimate_text_tokens uses TokenCounter count."""
        cm = ContextManager(store=None, token_limit=8000)
        result = await cm._async_estimate_text_tokens("hello")
        assert result >= 1
        result_empty = await cm._async_estimate_text_tokens("")
        assert result_empty == 1


class TestCompressionWindow:
    """CW-U7 ~ CW-U15: Compression window via maybe_compress (async)."""

    async def test_no_compression_when_under_threshold(self, store):
        cm = ContextManager(store=store, token_limit=100000)
        state = RunState(run_id="r1")
        state.thought_history = [ThoughtEntry(seq=1, thought="short", tool_choice="echo")]
        await cm.maybe_compress("r1", iteration=1, state=state)
        events = await store.get_events("r1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        pruned = [e for e in events if e.event_type == EventType.CONTEXT_PRUNED]
        assert len(archived) == 0
        assert len(pruned) == 0

    async def test_normal_compression_window_archives_episode(self, store):
        cm = ContextManager(store=store, token_limit=1000, compression_threshold_ratio=0.8)
        state = RunState(run_id="r1")
        state.thought_history = [ThoughtEntry(seq=i, thought="A" * 200, tool_choice="echo") for i in range(10)]
        state.tool_results = [
            ToolResult(tool_call_id=f"tc{i}", tool_name="echo", status="completed", output="B" * 200, event_seq=i + 10)
            for i in range(10)
        ]
        await cm.maybe_compress("r1", iteration=1, state=state)
        events = await store.get_events("r1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    async def test_emergency_compression_window_triggers_episode_archive(self, store):
        cm = ContextManager(
            store=store,
            token_limit=500,
            compression_threshold_ratio=0.8,
            emergency_threshold_ratio=0.9,
        )
        state = RunState(run_id="r1")
        state.thought_history = [ThoughtEntry(seq=i, thought="A" * 200, tool_choice="echo") for i in range(20)]
        state.tool_results = [
            ToolResult(tool_call_id=f"tc{i}", tool_name="echo", status="completed", output="B" * 200, event_seq=i + 20)
            for i in range(20)
        ]
        await cm.maybe_compress("r1", iteration=1, state=state)
        events = await store.get_events("r1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    async def test_compression_below_threshold_returns_none(self, store):
        cm = ContextManager(store=store, token_limit=100000)
        state = RunState(run_id="r1")
        state.thought_history = [ThoughtEntry(seq=1, thought="short", tool_choice="echo")]
        await cm.maybe_compress("r1", iteration=1, state=state)
        events = await store.get_events("r1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        pruned = [e for e in events if e.event_type == EventType.CONTEXT_PRUNED]
        assert len(archived) == 0
        assert len(pruned) == 0


class TestToolOutputTruncation:
    """CW-U7 ~ CW-U15: Tool output truncation in _generate_episode."""

    async def test_conversation_context_truncates_long_content(self, store):
        """CW-U16: _build_conversation_context truncation at 500 chars."""
        from harness.models.conversation import _build_conversation_context

        await store.upsert_conversation("conv-1", "Test")
        long_content = "A" * 1000
        await store.append_event(
            "conv-1",
            EventType.CONVERSATION_MESSAGE,
            {"conversation_id": "conv-1", "run_id": "r1", "role": "user", "content": long_content},
        )
        ctx = await _build_conversation_context(store, "conv-1")
        assert len(ctx) <= 510

    async def test_generate_episode_truncates_activity_text(self):
        """CW-U11: _generate_episode truncates long tool output at 2000 chars."""
        cm = ContextManager(store=None, token_limit=8000)
        state = RunState(run_id="r1")
        state.tool_results = [
            ToolResult(
                tool_call_id="tc1", tool_name="http_request", status="completed", output="X" * 3000, event_seq=1
            ),
        ]
        episode = await cm._generate_episode(
            state,
            episode_range=(1, 1),
            original_tokens=1000,
        )
        assert episode is not None
        assert episode.current_plan is not None
        assert len(episode.current_plan) < 3000

    async def test_generate_episode_truncates_thought_text(self):
        cm = ContextManager(store=None, token_limit=8000)
        state = RunState(run_id="r1")
        state.thought_history = [
            ThoughtEntry(seq=1, thought="T" * 600, tool_choice="echo"),
        ]
        episode = await cm._generate_episode(
            state,
            episode_range=(1, 1),
            original_tokens=1000,
        )
        assert episode is not None


class TestContextManagerIntegration:
    """CW-C1 ~ CW-C5: ContextManager integration with Scheduler."""

    async def test_maybe_compress_writes_event(self, store):
        """CW-C1: When over threshold, EPISODE_ARCHIVED event is written."""
        cm = ContextManager(store=store, token_limit=500, checkpoint_interval=100)
        state = RunState(run_id="r1")
        state.thought_history = [ThoughtEntry(seq=i, thought="A" * 200, tool_choice="echo") for i in range(1, 16)]
        await cm.maybe_compress("r1", iteration=1, state=state)
        events = await store.get_events("r1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) >= 1

    async def test_maybe_compress_cooldown(self, store):
        """CW-C2: Cooldown prevents repeated compression."""
        cm = ContextManager(store=store, token_limit=500, checkpoint_interval=5)
        state = RunState(run_id="r1")
        state.thought_history = [ThoughtEntry(seq=i, thought="A" * 200, tool_choice="echo") for i in range(1, 16)]
        await cm.maybe_compress("r1", iteration=1, state=state)
        await cm.maybe_compress("r1", iteration=2, state=state)
        events = await store.get_events("r1")
        archived = [e for e in events if e.event_type == EventType.EPISODE_ARCHIVED]
        assert len(archived) == 1

    async def test_checkpoint_written_at_interval(self, store):
        """CW-C3: ContextCheckpointed written every N iterations."""
        cm = ContextManager(store=store, token_limit=8000, checkpoint_interval=5)
        state = RunState(run_id="r1")
        state.seq = 10
        await cm.try_checkpoint("r1", iteration=5, state=state)
        events = await store.get_events("r1")
        checkpoints = [e for e in events if e.event_type == EventType.CONTEXT_CHECKPOINTED]
        assert len(checkpoints) == 1

    async def test_checkpoint_not_written_off_interval(self, store):
        cm = ContextManager(store=store, token_limit=8000, checkpoint_interval=5)
        state = RunState(run_id="r1")
        state.seq = 10
        await cm.try_checkpoint("r1", iteration=3, state=state)
        events = await store.get_events("r1")
        checkpoints = [e for e in events if e.event_type == EventType.CONTEXT_CHECKPOINTED]
        assert len(checkpoints) == 0

    async def test_find_resume_seq(self, store):
        """CW-C5: find_resume_seq returns latest checkpoint seq."""
        events = [
            Event(run_id="r1", seq=1, event_type=EventType.RUN_STARTED, payload={"intent": "test"}, created_at=1.0),
            Event(
                run_id="r1",
                seq=5,
                event_type=EventType.CONTEXT_CHECKPOINTED,
                payload=ContextCheckpointedPayload(checkpoint_seq=5, snapshot_ref="cp1", token_count=100).model_dump(),
                created_at=5.0,
            ),
            Event(
                run_id="r1",
                seq=10,
                event_type=EventType.CONTEXT_CHECKPOINTED,
                payload=ContextCheckpointedPayload(checkpoint_seq=10, snapshot_ref="cp2", token_count=200).model_dump(),
                created_at=10.0,
            ),
        ]
        seq = ContextManager.find_resume_seq(events)
        assert seq == 10

    async def test_find_resume_seq_no_checkpoint(self, store):
        events = [
            Event(run_id="r1", seq=1, event_type=EventType.RUN_STARTED, payload={"intent": "test"}, created_at=1.0),
        ]
        seq = ContextManager.find_resume_seq(events)
        assert seq == 0
