"""Root-cause regression for the 500 on GET /api/v1/runs bug.

Bug condition: legacy persistent DBs contain rows whose `event_type` value
refers to an enum member that was removed in a refactor (e.g.
`QualityCheckCompleted` from V0.8 in f63e474). The Append-Only invariant
forbids DELETE, so these orphan rows persist. The original `_row_to_event`
called `EventType(raw)` unconditionally, raising ValueError and crashing
the entire read query — `list_runs()` returned HTTP 500 for the demo DB.

Root cause per AGENTS.md §3.5:
  1. Root cause — enum refactor removed a member without a defense layer
     in the read path deserialization.
  2. Why existing mechanisms didn't catch it — no fallback at the trusted
     component boundary; `Append-Only` forbid DELETE of legacy rows but
     nothing tolerated them on read.
  3. How to prevent recurrence — every `_row_to_event` materializer now
     returns `Event | None` and skips unknown event_type values with a
     warning; list comprehensions filter None. A scenario test below
     pins the behavior.

Also covers the _run_to_conv cache eviction hook tied to scheduler
terminal state (technical debt from P0-04 fix).
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from harness.models.events import Event, EventType
from harness.storage.event_store import EventStore


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


async def _inject_row(store: EventStore, run_id: str, seq: int, event_type: str, payload: str = "{}") -> None:
    """Bypass append_event to plant a legacy row with a custom event_type string."""
    await store.conn.execute(
        "INSERT INTO events (run_id, seq, event_type, payload, idempotency_key, created_at) "
        "VALUES (?, ?, ?, ?, NULL, ?)",
        (run_id, seq, event_type, payload, time.time()),
    )
    await store.conn.commit()


class TestUnknownEventTypeTolerance:
    """The original bug: a single legacy row would crash the whole read path."""

    async def test_get_events_skips_legacy_row(self, store: EventStore):
        # One valid run event + one row with a removed enum member
        from harness.models.events import RunStartedPayload
        await store.append_event(
            "run-X", EventType.RUN_STARTED,
            RunStartedPayload(intent="i").model_dump(),
        )
        await _inject_row(store, "run-X", 2, "QualityCheckCompleted", '{"k":"v"}')

        events = await store.get_events("run-X")
        # Legacy row is skipped, valid RunStarted still returned
        assert len(events) == 1
        assert events[0].event_type == EventType.RUN_STARTED

    async def test_get_event_range_skips_legacy_row(self, store: EventStore):
        from harness.models.events import AgentThoughtPayload
        await store.append_event(
            "run-Y", EventType.RUN_STARTED,
            {"intent": "i", "context_snapshot": {}},
        )
        await _inject_row(store, "run-Y", 2, "QualityCheckCompleted")
        await store.append_event(
            "run-Y", EventType.AGENT_THOUGHT,
            AgentThoughtPayload(thought="t", token_count=1).model_dump(),
        )

        events = await store.get_event_range("run-Y", from_seq=1)
        # Two valid events returned; the legacy seq=2 row is dropped
        assert len(events) == 2
        types = [e.event_type for e in events]
        assert EventType.RUN_STARTED in types
        assert EventType.AGENT_THOUGHT in types

    async def test_get_events_for_runs_skips_legacy_row(self, store: EventStore):
        from harness.models.events import RunStartedPayload
        await store.append_event(
            "run-Z", EventType.RUN_STARTED,
            RunStartedPayload(intent="i").model_dump(),
        )
        await _inject_row(store, "run-Z", 2, "SomethingRemoved")

        events = await store.get_events_for_runs(["run-Z"])
        assert len(events) == 1
        assert events[0].event_type == EventType.RUN_STARTED

    async def test_find_by_idempotency_key_returns_none_for_legacy_row(self, store: EventStore):
        """A legacy row that matches the idempotency-key lookup signature must
        not crash find_by_idempotency_key; rather it is treated as no hit.
        """
        # Plant a row with the removed event_type but matching key constraints
        await store.conn.execute(
            "INSERT INTO events (run_id, seq, event_type, payload, idempotency_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("run-W", 1, "QualityCheckCompleted", "{}", "ik-1", time.time()),
        )
        await store.conn.commit()
        # Looking up by a known current EventType should not find the legacy row
        result = await store.find_by_idempotency_key(
            "run-W", EventType.RUN_STARTED, "ik-1"
        )
        # No hit because event_type doesn't match EventType.RUN_STARTED.value
        assert result is None

    async def test_find_confirmation_by_id_skips_legacy_row(self, store: EventStore):
        from harness.models.events import (
            ConfirmationReceivedPayload,
        )
        # Plant an unrelated legacy row on the same run first
        await _inject_row(store, "run-C", 1, "QualityCheckCompleted")

        # Then append a valid CONFIRMATION_RECEIVED for the same run
        await store.append_event(
            "run-C", EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id="c1", confirmed=True, operator_id="op"
            ).model_dump(),
            idempotency_key="confirm_c1",
        )

        result = await store.find_confirmation_by_id("run-C", "c1")
        assert result is not None
        assert result.event_type == EventType.CONFIRMATION_RECEIVED


class TestUnknownEventLegacyPersistence:
    """A legacy persistent DB (pre-date the column / new enum members) must
    still load via initialize + get_events without crashing.
    """

    async def test_legacy_persistent_db_loads(self):
        tmp = Path(tempfile.gettempdir()) / "opencode" / "legacy_quality.db"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            tmp.unlink()
        # Build an old-shape DB with the removed enum member persisted
        c = sqlite3.connect(str(tmp))
        c.executescript(
            "CREATE TABLE events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "event_type TEXT NOT NULL, payload TEXT NOT NULL, idempotency_key TEXT, "
            "created_at REAL NOT NULL, PRIMARY KEY (run_id, seq));"
            "CREATE UNIQUE INDEX idx_idem ON events(run_id, event_type, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL;"
        )
        c.execute(
            "INSERT INTO events VALUES "
            "('run-L', 1, 'QualityCheckCompleted', '{}', NULL, 1.0)"
        )
        c.commit()
        c.close()

        s = EventStore(str(tmp))
        await s.initialize()  # must not crash
        events = await s.get_events("run-L")
        # Legacy row skipped, empty list returned, no exception
        assert events == []
        await s.close()
        tmp.unlink(missing_ok=True)


class TestQueryPathUnknownEventType:
    """api/query._row_to_event and analysis/service._row_to_event parity."""

    async def test_api_query_row_to_event_returns_none_for_legacy(self, store: EventStore):
        from harness.api.query import _row_to_event
        row = {
            "run_id": "r1", "seq": 1, "event_type": "QualityCheckCompleted",
            "payload": "{}", "idempotency_key": None, "created_at": 1.0,
        }
        assert _row_to_event(row) is None

    async def test_analysis_service_row_to_event_returns_none_for_legacy(self, store: EventStore):
        from harness.analysis.service import AnalysisService
        row = {
            "run_id": "r1", "seq": 1, "event_type": "QualityCheckCompleted",
            "payload": "{}", "idempotency_key": None, "created_at": 1.0,
        }
        assert AnalysisService._row_to_event(row) is None


class TestRunToConvCacheEviction:
    """P0-04 follow-up — _run_to_conv cache must be evictable when a run ends.

    The scheduler terminal hook (BaseScheduler._run_end_cb) calls
    store.evict_run_to_conv(run_id) so that the in-memory mapping doesn't
    grow unbounded across runs.
    """

    async def test_evict_drops_cache_entry(self, store: EventStore):
        from harness.models.events import RunStartedPayload
        await store.append_event(
            "run-E", EventType.RUN_STARTED,
            RunStartedPayload(intent="i", conversation_id="conv-E").model_dump(),
        )
        assert store._run_to_conv.get("run-E") == "conv-E"
        store.evict_run_to_conv("run-E")
        assert "run-E" not in store._run_to_conv

    async def test_evict_does_not_break_subsequent_appends_without_conv(self, store: EventStore):
        """After eviction, appending another event on the same run_id without
        conversation_id in payload should not crash; column will be NULL."""
        from harness.models.events import AgentThoughtPayload
        await store.append_event(
            "run-F", EventType.RUN_STARTED,
            {"intent": "i", "context_snapshot": {}, "conversation_id": "conv-F"},
        )
        store.evict_run_to_conv("run-F")
        # Subsequent append: no cache, payload has no conversation_id
        await store.append_event(
            "run-F", EventType.AGENT_THOUGHT,
            AgentThoughtPayload(thought="t", token_count=1).model_dump(),
        )
        # The AGENT_THOUGHT row should have NULL conversation_id column
        cursor = await store.conn.execute(
            "SELECT conversation_id FROM events WHERE run_id = ? AND event_type = ?",
            ("run-F", EventType.AGENT_THOUGHT.value),
        )
        row = await cursor.fetchone()
        assert row["conversation_id"] is None