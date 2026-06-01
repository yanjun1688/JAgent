from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from harness.models.events import EventType
from harness.storage.event_store import EventStore

pytestmark = pytest.mark.asyncio


class TestAppendEvent:
    async def test_append_first_event(self, store: EventStore):
        event = await store.append_event(
            "run-1",
            EventType.RUN_STARTED,
            {"intent": "search github for opencode", "context_snapshot": {}},
        )
        assert event.run_id == "run-1"
        assert event.seq == 1
        assert event.event_type == EventType.RUN_STARTED
        assert event.payload["intent"] == "search github for opencode"

    async def test_seq_increments_per_run(self, store: EventStore):
        e1 = await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "task A", "context_snapshot": {}}
        )
        e2 = await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "thinking...", "token_count": 42},
        )
        assert e1.seq == 1
        assert e2.seq == 2

    async def test_seq_independent_per_run(self, store: EventStore):
        await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "A", "context_snapshot": {}}
        )
        await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "x", "token_count": 1},
        )
        e = await store.append_event(
            "run-2", EventType.RUN_STARTED, {"intent": "B", "context_snapshot": {}}
        )
        assert e.seq == 1

    async def test_payload_validation_rejects_invalid(self, store: EventStore):
        with pytest.raises(Exception):
            await store.append_event(
                "run-1",
                EventType.RUN_STARTED,
                {"wrong_field": 123},
            )

    async def test_idempotency_key_returns_existing(self, store: EventStore):
        payload = {
            "tool_call_id": "tc-1",
            "tool_name": "http",
            "input": {"url": "x"},
            "idempotency_key": "ik-abc",
        }
        e1 = await store.append_event(
            "run-1",
            EventType.TOOL_CALLED,
            payload,
            idempotency_key="ik-abc",
        )
        e2 = await store.append_event(
            "run-1",
            EventType.TOOL_CALLED,
            payload,
            idempotency_key="ik-abc",
        )
        assert e1.seq == e2.seq
        assert e1.run_id == e2.run_id

    async def test_idempotency_same_key_different_event_type_allowed(self, store: EventStore):
        e1 = await store.append_event(
            "run-1",
            EventType.TOOL_CALLED,
            {
                "tool_call_id": "tc-x",
                "tool_name": "http",
                "input": {"url": "x"},
                "idempotency_key": "ik-xyz",
            },
            idempotency_key="ik-xyz",
        )
        e2 = await store.append_event(
            "run-1",
            EventType.TOOL_COMPLETED,
            {
                "tool_call_id": "tc-x",
                "tool_name": "http",
                "output": "ok",
                "duration_ms": 100,
            },
            idempotency_key="ik-xyz",
        )
        assert e1.seq != e2.seq

    async def test_null_idempotency_key_not_checked(self, store: EventStore):
        e1 = await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "a", "token_count": 1},
        )
        e2 = await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "b", "token_count": 1},
        )
        assert e1.seq == 1
        assert e2.seq == 2

    async def test_run_completed(self, store: EventStore):
        await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}
        )
        await store.append_event(
            "run-1",
            EventType.RUN_COMPLETED,
            {"result_summary": "done"},
        )
        events = await store.get_events("run-1")
        assert len(events) == 2
        assert events[1].event_type == EventType.RUN_COMPLETED

    async def test_run_failed(self, store: EventStore):
        await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}
        )
        await store.append_event(
            "run-1",
            EventType.RUN_FAILED,
            {"final_error": "crash", "event_count": 2},
        )
        events = await store.get_events("run-1")
        assert len(events) == 2
        assert events[1].event_type == EventType.RUN_FAILED


class TestGetEvents:
    async def test_returns_empty_for_unknown_run(self, store: EventStore):
        events = await store.get_events("nonexistent")
        assert events == []

    async def test_returns_in_seq_order(self, store: EventStore):
        await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}
        )
        await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "first", "token_count": 1},
        )
        await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "second", "token_count": 1},
        )

        events = await store.get_events("run-1")
        assert [e.seq for e in events] == [1, 2, 3]
        assert [e.payload["thought"] for e in events[1:]] == ["first", "second"]

    async def test_payload_is_dict_with_correct_types(self, store: EventStore):
        await store.append_event(
            "run-1",
            EventType.TOOL_COMPLETED,
            {
                "tool_call_id": "tc-1",
                "tool_name": "http",
                "output": {"status": 200},
                "duration_ms": 150,
            },
        )
        events = await store.get_events("run-1")
        payload = events[0].payload
        assert payload["tool_name"] == "http"
        assert payload["output"] == {"status": 200}
        assert payload["duration_ms"] == 150
        assert payload["tool_call_id"] == "tc-1"


class TestGetEventRange:
    async def test_returns_subset(self, store: EventStore):
        for i in range(5):
            await store.append_event(
                "run-1",
                EventType.AGENT_THOUGHT,
                {"thought": f"t{i}", "token_count": i},
            )

        events = await store.get_event_range("run-1", from_seq=2, to_seq=4)
        assert len(events) == 3
        assert [e.seq for e in events] == [2, 3, 4]

    async def test_from_seq_only(self, store: EventStore):
        for i in range(3):
            await store.append_event(
                "run-1",
                EventType.AGENT_THOUGHT,
                {"thought": f"t{i}", "token_count": i},
            )

        events = await store.get_event_range("run-1", from_seq=2)
        assert len(events) == 2
        assert [e.seq for e in events] == [2, 3]

    async def test_empty_range(self, store: EventStore):
        await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "t", "token_count": 1},
        )
        events = await store.get_event_range("run-1", from_seq=5, to_seq=10)
        assert events == []

    async def test_from_seq_greater_than_to_seq(self, store: EventStore):
        await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "t", "token_count": 1},
        )
        events = await store.get_event_range("run-1", from_seq=3, to_seq=1)
        assert events == []


class TestGetLatestSeq:
    async def test_returns_zero_for_empty_run(self, store: EventStore):
        seq = await store.get_latest_seq("run-1")
        assert seq == 0

    async def test_returns_max_seq(self, store: EventStore):
        await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}
        )
        await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "t", "token_count": 1},
        )
        await store.append_event(
            "run-1",
            EventType.RUN_COMPLETED,
            {"result_summary": "ok"},
        )
        seq = await store.get_latest_seq("run-1")
        assert seq == 3


class TestEventCount:
    async def test_counts_events(self, store: EventStore):
        assert await store.event_count("run-1") == 0
        await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}
        )
        await store.append_event(
            "run-1",
            EventType.AGENT_THOUGHT,
            {"thought": "t", "token_count": 1},
        )
        assert await store.event_count("run-1") == 2


class TestContextManager:
    async def test_async_context_manager(self):
        async with EventStore(":memory:") as store:
            event = await store.append_event(
                "run-1",
                EventType.RUN_STARTED,
                {"intent": "test", "context_snapshot": {}},
            )
            assert event.seq == 1
        assert store._conn is None

    async def test_initialize_is_idempotent(self, store: EventStore):
        await store.append_event(
            "run-1", EventType.RUN_STARTED, {"intent": "a", "context_snapshot": {}}
        )
        await store.initialize()
        events = await store.get_events("run-1")
        assert len(events) == 1

    async def test_file_path_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "subdir" / "test.db")
            async with EventStore(db_path) as store:
                event = await store.append_event(
                    "run-1",
                    EventType.RUN_STARTED,
                    {"intent": "test", "context_snapshot": {}},
                )
                assert event.seq == 1


class TestAll14EventTypes:
    async def test_all_payload_types_write_and_read(self, store: EventStore):
        test_cases = [
            (EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {"key": "val"}}),
            (EventType.AGENT_THOUGHT, {"thought": "hmm", "tool_choice": "http", "token_count": 100}),
            (EventType.TOOL_CALLED, {"tool_call_id": "tc-1", "tool_name": "http", "input": {"url": "https://x.com"}, "idempotency_key": "ik-1"}),
            (EventType.TOOL_COMPLETED, {"tool_call_id": "tc-1", "tool_name": "http", "output": {"status": 200}, "duration_ms": 50}),
            (EventType.TOOL_FAILED, {"tool_call_id": "tc-2", "tool_name": "http", "error": "timeout", "retryable": True}),
            (EventType.TOOL_TIMEOUT, {"tool_call_id": "tc-3", "tool_name": "http", "timeout_ms": 5000}),
            (EventType.GUARDRAIL_TRIGGERED, {"tool_call_id": "tc-4", "tool_name": "file_op", "guardrail_id": "scope", "reason": "out of bounds"}),
            (EventType.CONFIRMATION_REQUESTED, {"confirmation_id": "cf-1", "tool_name": "file_op", "input": {"path": "/etc"}, "risk_level": "high"}),
            (EventType.CONFIRMATION_RECEIVED, {"confirmation_id": "cf-1", "confirmed": True, "operator_id": "op-1"}),
            (EventType.CONTEXT_COMPRESSED, {"original_tokens": 10000, "compressed_tokens": 2000, "summary_ref": "sum-1"}),
            (EventType.RUN_PAUSED, {"reason": "waiting for confirmation"}),
            (EventType.RUN_RESUMED, {"resume_from_seq": 5}),
            (EventType.RUN_COMPLETED, {"result_summary": "all done"}),
            (EventType.RUN_FAILED, {"final_error": "max retries exceeded", "event_count": 10}),
        ]

        for idx, (etype, payload) in enumerate(test_cases, start=1):
            event = await store.append_event("run-1", etype, payload)
            assert event.seq == idx
            assert event.event_type == etype

        events = await store.get_events("run-1")
        assert len(events) == 14
