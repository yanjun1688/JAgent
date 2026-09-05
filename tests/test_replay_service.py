"""Unit tests for ReplayInspectorService (read-only application service).

Covers behaviour not exercised over HTTP: trace-URL provider injection
(reserved Langfuse seam) and the typed not-found / out-of-range exceptions.
"""

from __future__ import annotations

import pytest

from harness.replay.service import (
    ReplayInspectorService,
    ReplayRunNotFoundError,
    ReplaySeqOutOfRangeError,
)
from harness.storage.event_store import EventStore

from .replay_fixtures import seed_failed_plan_run


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


class TestServiceStateAndExceptions:
    async def test_state_at_unknown_run_raises_not_found(self, store):
        svc = ReplayInspectorService(store)
        with pytest.raises(ReplayRunNotFoundError):
            await svc.get_state_at("ghost")

    async def test_state_at_out_of_range_raises(self, store):
        await seed_failed_plan_run(store, "r1")
        svc = ReplayInspectorService(store)
        with pytest.raises(ReplaySeqOutOfRangeError):
            await svc.get_state_at("r1", at_seq=999)

    async def test_diff_inverted_raises(self, store):
        await seed_failed_plan_run(store, "r1")
        svc = ReplayInspectorService(store)
        with pytest.raises(ReplaySeqOutOfRangeError):
            await svc.get_diff("r1", from_seq=10, to_seq=2)

    async def test_meta_unknown_run_returns_none(self, store):
        svc = ReplayInspectorService(store)
        assert await svc.get_run_meta("ghost") is None


class TestTraceUrlProvider:
    async def test_provider_result_is_surfaced_in_meta(self, store):
        # Given a service wired with a Langfuse link provider (future seam)
        await seed_failed_plan_run(store, "r1")
        svc = ReplayInspectorService(store, trace_url_provider=lambda rid: f"https://langfuse.example/traces/{rid}")
        # When reading meta
        meta = await svc.get_run_meta("r1")
        # Then the reserved link is populated
        assert meta.langfuse_trace_url == "https://langfuse.example/traces/r1"

    async def test_provider_returning_none_leaves_field_null(self, store):
        await seed_failed_plan_run(store, "r1")
        svc = ReplayInspectorService(store, trace_url_provider=lambda rid: None)
        meta = await svc.get_run_meta("r1")
        assert meta.langfuse_trace_url is None

    async def test_no_provider_leaves_field_null(self, store):
        await seed_failed_plan_run(store, "r1")
        svc = ReplayInspectorService(store)
        meta = await svc.get_run_meta("r1")
        assert meta.langfuse_trace_url is None
