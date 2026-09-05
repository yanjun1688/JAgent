"""Read-only service for the Event Replay Inspector.

The service is the *application* seam: it loads event slices through the
tenant-scoped store (``ScopedEventStore``) and hands them to the pure
projection functions in :mod:`harness.replay.projection`. It performs no
folding/state derivation of its own and never calls a write/execution path.

Read-only guarantee (statically auditable): this module imports only
``harness.storage`` (read methods), ``harness.replay`` (pure projection +
schemas) and stdlib. It does NOT import the scheduler, tool executor, tools,
execution backends, monitoring/writers, or lifecycle. A test
(``tests/test_replay_api.py::test_replay_package_imports_are_read_only``)
asserts this so the boundary cannot silently erode.

Future rollback/fork will add a *separate* write service beside this one; this
service and ``projection.reconstruct_state`` stay untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from harness.replay.projection import diff_states, project_state_view, project_timeline_event
from harness.replay.schemas import (
    ReplayRunMeta,
    ReplayTimelineResponse,
    RunStateView,
    StateDiff,
)
from harness.storage.event_store import EventStore


class ReplayRunNotFoundError(LookupError):
    """The run does not exist within the current tenant scope (-> HTTP 404)."""


class ReplaySeqOutOfRangeError(ValueError):
    """Requested seq is outside the run's event range (-> HTTP 400)."""


# A provider that maps run_id -> Langfuse trace URL, or None when unavailable.
# Injected by the API layer (which owns the tracer); this service stays free of
# any monitoring import. Reserved for the future Langfuse cross-reference.
TraceUrlProvider = Callable[[str], str | None]


class ReplayInspectorService:
    def __init__(self, store: EventStore, trace_url_provider: TraceUrlProvider | None = None) -> None:
        self._store = store
        self._trace_url = trace_url_provider

    # -- Run metadata --

    async def get_run_meta(self, run_id: str) -> ReplayRunMeta | None:
        events = await self._store.get_events(run_id)
        if not events:
            return None
        latest_seq = max(e.seq for e in events)
        # Status/intent come from the canonical fold (never re-derived here).
        state = project_state_view(events, at_seq=latest_seq, latest_seq=latest_seq)
        return ReplayRunMeta(
            run_id=run_id,
            status=state.status,
            intent=state.intent,
            latest_seq=latest_seq,
            event_count=len(events),
            created_at=events[0].created_at,
            langfuse_trace_url=self._trace_url(run_id) if self._trace_url else None,
        )

    # -- Timeline --

    async def get_timeline(self, run_id: str, cursor: int = 0, limit: int = 200) -> ReplayTimelineResponse | None:
        events = await self._store.get_events(run_id)
        if not events:
            return None
        total = len(events)
        start = max(cursor, 0)
        end = min(start + limit, total)
        page = events[start:end]
        return ReplayTimelineResponse(
            run_id=run_id,
            latest_seq=events[-1].seq,
            total=total,
            timeline=[project_timeline_event(e) for e in page],
            next_cursor=end if end < total else 0,
            has_more=end < total,
        )

    # -- State at a point in time --

    async def get_state_at(self, run_id: str, at_seq: int | None = None) -> RunStateView:
        events = await self._store.get_events(run_id)
        if not events:
            raise ReplayRunNotFoundError(run_id)
        first_seq, last_seq = events[0].seq, events[-1].seq
        target = last_seq if at_seq is None else at_seq
        self._validate_seq(target, first_seq, last_seq)
        return project_state_view(events, at_seq=target, latest_seq=last_seq)

    # -- Diff between two points in time --

    async def get_diff(self, run_id: str, from_seq: int, to_seq: int) -> StateDiff:
        events = await self._store.get_events(run_id)
        if not events:
            raise ReplayRunNotFoundError(run_id)
        first_seq, last_seq = events[0].seq, events[-1].seq
        if from_seq > to_seq:
            raise ReplaySeqOutOfRangeError(f"from_seq ({from_seq}) must be <= to_seq ({to_seq})")
        self._validate_seq(from_seq, first_seq, last_seq)
        self._validate_seq(to_seq, first_seq, last_seq)
        return diff_states(events, from_seq=from_seq, to_seq=to_seq)

    # -- Internal --

    @staticmethod
    def _validate_seq(seq: int, first_seq: int, last_seq: int) -> None:
        # Bounds are the tenant-scoped stream's *visible* seq range. A run may
        # have events owned by another tenant (stored under the same run_id);
        # those rows are filtered out by ScopedEventStore, so the earliest
        # visible seq can be > 1. Requesting a point before it (or after the
        # end) must be a clean 400, not a fold-on-empty-stream 500.
        if seq < first_seq or seq > last_seq:
            raise ReplaySeqOutOfRangeError(
                f"seq {seq} is out of range for this run (visible range: {first_seq}..{last_seq})"
            )


__all__: Sequence[str] = (
    "ReplayInspectorService",
    "ReplayRunNotFoundError",
    "ReplaySeqOutOfRangeError",
    "TraceUrlProvider",
)
