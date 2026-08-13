from __future__ import annotations

import pytest

from harness.core.fold import RunStatus, fold_events
from harness.core.lifecycle import mark_orphans
from harness.models.events import Event, EventType
from harness.storage.event_store import EventStore


def _event(
    run_id: str,
    seq: int,
    event_type: EventType,
    payload: dict,
) -> Event:
    return Event(
        run_id=run_id,
        seq=seq,
        event_type=event_type,
        payload=payload,
        created_at=0.0,
    )


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


class TestFoldOrphaned:
    def test_orphaned_default_false(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
        ]
        state = fold_events(events)
        assert state.orphaned is False

    def test_run_orphaned_sets_flag(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_ORPHANED, {"reason": "server_restart", "detected_at": 1.0}),
        ]
        state = fold_events(events)
        assert state.orphaned is True
        assert state.status == RunStatus.RUNNING

    def test_orphaned_preserves_status_running(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_ORPHANED, {"reason": "server_restart", "detected_at": 1.0}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.RUNNING
        assert state.orphaned is True

    def test_orphaned_preserves_status_paused(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_PAUSED, {"reason": "user_requested"}),
            _event("r1", 3, EventType.RUN_ORPHANED, {"reason": "server_restart", "detected_at": 1.0}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.PAUSED
        assert state.orphaned is True

    def test_multiple_orphaned_events_idempotent(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_ORPHANED, {"reason": "server_restart", "detected_at": 1.0}),
            _event("r1", 3, EventType.RUN_ORPHANED, {"reason": "server_restart", "detected_at": 2.0}),
        ]
        state = fold_events(events)
        assert state.orphaned is True


class TestMarkOrphans:
    async def test_marks_running_run(self, store: EventStore):
        await store.append_event(
            "r1",
            EventType.RUN_STARTED,
            {"intent": "test", "context_snapshot": {}},
        )
        marked = await mark_orphans(store)
        assert marked == 1

        events = await store.get_events("r1")
        orphaned_events = [e for e in events if e.event_type == EventType.RUN_ORPHANED]
        assert len(orphaned_events) == 1

    async def test_marks_paused_run(self, store: EventStore):
        await store.append_event(
            "r1",
            EventType.RUN_STARTED,
            {"intent": "test", "context_snapshot": {}},
        )
        await store.append_event(
            "r1",
            EventType.RUN_PAUSED,
            {"reason": "user_requested"},
        )
        marked = await mark_orphans(store)
        assert marked == 1

    async def test_does_not_mark_completed(self, store: EventStore):
        await store.append_event(
            "r1",
            EventType.RUN_STARTED,
            {"intent": "test", "context_snapshot": {}},
        )
        await store.append_event(
            "r1",
            EventType.RUN_COMPLETED,
            {"result_summary": "done"},
        )
        marked = await mark_orphans(store)
        assert marked == 0

        events = await store.get_events("r1")
        orphaned_events = [e for e in events if e.event_type == EventType.RUN_ORPHANED]
        assert len(orphaned_events) == 0

    async def test_does_not_mark_failed(self, store: EventStore):
        await store.append_event(
            "r1",
            EventType.RUN_STARTED,
            {"intent": "test", "context_snapshot": {}},
        )
        await store.append_event(
            "r1",
            EventType.RUN_FAILED,
            {"final_error": "boom", "event_count": 2},
        )
        marked = await mark_orphans(store)
        assert marked == 0

    async def test_idempotent_no_double_mark(self, store: EventStore):
        await store.append_event(
            "r1",
            EventType.RUN_STARTED,
            {"intent": "test", "context_snapshot": {}},
        )
        marked1 = await mark_orphans(store)
        assert marked1 == 1

        marked2 = await mark_orphans(store)
        assert marked2 == 0

        events = await store.get_events("r1")
        orphaned_events = [e for e in events if e.event_type == EventType.RUN_ORPHANED]
        assert len(orphaned_events) == 1

    async def test_multiple_runs_mixed(self, store: EventStore):
        await store.append_event(
            "r1",
            EventType.RUN_STARTED,
            {"intent": "running task", "context_snapshot": {}},
        )
        await store.append_event(
            "r2",
            EventType.RUN_STARTED,
            {"intent": "completed task", "context_snapshot": {}},
        )
        await store.append_event(
            "r2",
            EventType.RUN_COMPLETED,
            {"result_summary": "done"},
        )
        await store.append_event(
            "r3",
            EventType.RUN_STARTED,
            {"intent": "paused task", "context_snapshot": {}},
        )
        await store.append_event(
            "r3",
            EventType.RUN_PAUSED,
            {"reason": "user_requested"},
        )

        marked = await mark_orphans(store)
        assert marked == 2

        r1_events = await store.get_events("r1")
        r1_orphaned = [e for e in r1_events if e.event_type == EventType.RUN_ORPHANED]
        assert len(r1_orphaned) == 1

        r2_events = await store.get_events("r2")
        r2_orphaned = [e for e in r2_events if e.event_type == EventType.RUN_ORPHANED]
        assert len(r2_orphaned) == 0

        r3_events = await store.get_events("r3")
        r3_orphaned = [e for e in r3_events if e.event_type == EventType.RUN_ORPHANED]
        assert len(r3_orphaned) == 1

    async def test_empty_store(self, store: EventStore):
        marked = await mark_orphans(store)
        assert marked == 0

    async def test_fold_after_mark_shows_orphaned(self, store: EventStore):
        await store.append_event(
            "r1",
            EventType.RUN_STARTED,
            {"intent": "test", "context_snapshot": {}},
        )
        await mark_orphans(store)

        events = await store.get_events("r1")
        state = fold_events(events)
        assert state.orphaned is True
        assert state.status == RunStatus.RUNNING
