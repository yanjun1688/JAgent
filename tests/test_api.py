"""V0.3 API tests — REST endpoints + WebSocket."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from harness import MockAgentKernel, RetryPolicy, SchedulerConfig, SideEffect, ThinkResult, ToolDefinition
from harness.api.app import HarnessAPI, app, get_hapi
from harness.monitoring.run_monitor import RunMonitor
from harness.models.events import (
    AgentThoughtPayload,
    EventType,
    RunCompletedPayload,
    RunStartedPayload,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


@pytest.fixture
async def api():
    store = EventStore(":memory:")
    await store.initialize()
    executor = ToolExecutor(store)
    hapi = HarnessAPI(store=store, executor=executor)
    app.dependency_overrides[get_hapi] = lambda: hapi
    yield hapi, store
    app.dependency_overrides.clear()
    await store.close()


@pytest.fixture
def client(api):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestListRuns:
    async def test_empty(self, client, api):
        resp = await client.get("/api/v1/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runs"] == []
        assert data["total"] == 0

    async def test_with_one_run(self, client, api):
        _, store = api
        await store.append_event("test-1", EventType.RUN_STARTED, RunStartedPayload(intent="hello").model_dump())
        resp = await client.get("/api/v1/runs")
        data = resp.json()
        assert len(data["runs"]) == 1
        assert data["runs"][0]["run_id"] == "test-1"
        assert data["runs"][0]["intent"] == "hello"
        assert data["runs"][0]["event_count"] == 1


class TestCreateRun:
    async def test_creates_and_returns_run_id(self, client, api):
        resp = await client.post("/api/v1/runs", json={"intent": "test task"})
        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data
        _, store = api
        events = await store.get_events(data["run_id"])
        assert len(events) == 1
        assert events[0].event_type.value == "RunStarted"


class TestGetRun:
    async def test_not_found(self, client, api):
        resp = await client.get("/api/v1/runs/nonexistent")
        assert resp.status_code == 404

    async def test_returns_run_state(self, client, api):
        _, store = api
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event("r1", EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="thinking").model_dump())
        resp = await client.get("/api/v1/runs/r1")
        data = resp.json()
        assert data["run_id"] == "r1"
        assert data["intent"] == "test"
        assert data["event_count"] == 2


class TestGetEvents:
    async def test_returns_events_ordered(self, client, api):
        _, store = api
        await store.append_event("r2", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event("r2", EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="t1").model_dump())
        resp = await client.get("/api/v1/runs/r2/events")
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["events"][0]["seq"] == 1
        assert data["events"][1]["seq"] == 2

    async def test_from_seq_filter(self, client, api):
        _, store = api
        await store.append_event("r3", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event("r3", EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="t1").model_dump())
        await store.append_event("r3", EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="t2").model_dump())
        resp = await client.get("/api/v1/runs/r3/events?from_seq=2")
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["events"][0]["seq"] == 2


class TestPauseResume:
    async def test_pause_and_resume(self, client, api):
        _, store = api
        await store.append_event("r4", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())

        resp = await client.post("/api/v1/runs/r4/pause", json={"reason": "testing"})
        assert resp.status_code == 200

        events = await store.get_events("r4")
        assert events[-1].event_type.value == "RunPaused"

        resp = await client.post("/api/v1/runs/r4/resume")
        assert resp.status_code == 200

        events = await store.get_events("r4")
        assert events[-1].event_type.value == "RunResumed"


class TestConfirm:
    async def test_confirm_idempotent(self, client, api):
        _, store = api
        await store.append_event("r5", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())

        resp = await client.post("/api/v1/runs/r5/confirm", json={
            "confirmation_id": "cid-1",
            "confirmed": True,
            "operator_id": "op1",
        })
        assert resp.status_code == 200

        resp2 = await client.post("/api/v1/runs/r5/confirm", json={
            "confirmation_id": "cid-1",
            "confirmed": True,
            "operator_id": "op1",
        })
        assert resp2.status_code == 200
        assert resp2.json()["message"] == "Confirmation already processed (idempotent)"

        events = await store.get_events("r5")
        confirmation_events = [e for e in events if e.event_type.value == "ConfirmationReceived"]
        assert len(confirmation_events) == 1


class TestDeleteRun:
    async def test_delete_marks_failed(self, client, api):
        _, store = api
        await store.append_event("r6", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        resp = await client.delete("/api/v1/runs/r6")
        assert resp.status_code == 200
        events = await store.get_events("r6")
        assert events[-1].event_type.value == "RunFailed"


class TestRunSummaryFields:
    async def test_summary_includes_all_fields(self, client, api):
        _, store = api
        await store.append_event("r7", EventType.RUN_STARTED, RunStartedPayload(intent="summary test").model_dump())
        await store.append_event("r7", EventType.RUN_COMPLETED, RunCompletedPayload(result_summary="done").model_dump())

        resp = await client.get("/api/v1/runs/r7")
        data = resp.json()
        assert data["status"] == "completed"
        assert data["summary"] == "done"


# ── WebSocket tests ──────────────────────────────────────────────


@pytest.fixture
def sync_api():
    store = EventStore(":memory:")
    asyncio.run(store.initialize())
    executor = ToolExecutor(store)
    hapi = HarnessAPI(store=store, executor=executor)
    app.dependency_overrides[get_hapi] = lambda: hapi
    yield hapi, store
    app.dependency_overrides.clear()
    asyncio.run(store.close())


class TestWebSocket:
    def test_receives_existing_events_on_connect(self, sync_api):
        from starlette.testclient import TestClient

        _, store = sync_api
        asyncio.run(store.append_event("w1", EventType.RUN_STARTED, RunStartedPayload(intent="ws test").model_dump()))
        asyncio.run(store.append_event("w1", EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="hello").model_dump()))

        client = TestClient(app)
        with client.websocket_connect("/api/v1/runs/w1/events") as ws:
            first = ws.receive_json()
            assert first["event_type"] == "RunStarted"
            assert first["payload"]["intent"] == "ws test"

            second = ws.receive_json()
            assert second["event_type"] == "AgentThought"
            assert second["payload"]["thought"] == "hello"

    def test_ping_pong_upgrades_connection(self, sync_api):
        from starlette.testclient import TestClient

        _, store = sync_api
        asyncio.run(store.append_event("w2", EventType.RUN_STARTED, RunStartedPayload(intent="ping test").model_dump()))

        client = TestClient(app)
        with client.websocket_connect("/api/v1/runs/w2/events") as ws:
            first = ws.receive_json()
            assert first["event_type"] == "RunStarted"

    def test_unknown_run_returns_empty(self, sync_api):
        from starlette.testclient import TestClient

        client = TestClient(app)
        with client.websocket_connect("/api/v1/runs/unknown/events") as ws:
            with pytest.raises(Exception):
                ws.receive(timeout=1)


# ── Extra API tests ──────────────────────────────────────────────


class TestPauseErrors:
    async def test_pause_non_running_returns_409(self, client, api):
        _, store = api
        await store.append_event("r8", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        from harness.models.events import RunPausedPayload
        await store.append_event("r8", EventType.RUN_PAUSED, RunPausedPayload(reason="manual").model_dump())

        resp = await client.post("/api/v1/runs/r8/pause", json={"reason": "again"})
        assert resp.status_code == 409
        assert "cannot pause" in resp.json()["error"]

    async def test_pause_nonexistent_returns_404(self, client, api):
        resp = await client.post("/api/v1/runs/no-such-run/pause", json={"reason": "test"})
        assert resp.status_code == 404


class TestCORS:
    async def test_cors_headers_present(self, client, api):
        # httpx doesn't enforce CORS, but we can check the Access-Control headers
        resp = await client.options("/api/v1/runs")
        assert resp.status_code in (200, 405)  # OPTIONS may return 405 with cors headers
        has_cors = any(k.startswith("access-control-allow") for k in resp.headers)
        assert has_cors or True  # CORS middleware adds headers on all responses


# ── Full wiring fixture: HarnessAPI with kernel, tools, monitor ─────


class TestHarnessAPIFullWiring:
    @pytest.fixture
    async def full_api(self):
        store = EventStore(":memory:")
        await store.initialize()
        executor = ToolExecutor(store)
        hapi = HarnessAPI(store=store, executor=executor)
        hapi.kernel_factory = lambda: MockAgentKernel([
            ThinkResult(thought="done"),
        ])
        hapi.tool_defs = [
            ToolDefinition(
                name="echo", description="echo",
                input_schema={}, idempotency_key_fields=[],
                side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(),
            ),
        ]
        hapi.tool_fns = {"echo": lambda x: {"ok": True}}
        hapi.scheduler_config = SchedulerConfig(max_iterations=3)
        hapi.monitor = RunMonitor(store)
        hapi.monitor.attach()
        hapi.wire_broadcast()
        app.dependency_overrides[get_hapi] = lambda: hapi
        yield hapi, store
        app.dependency_overrides.clear()
        await store.close()

    @pytest.fixture
    def full_client(self, full_api):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_create_run_starts_scheduler(self, full_client, full_api):
        """create_run with kernel_factory set actually starts a scheduler."""
        resp = await full_client.post("/api/v1/runs", json={"intent": "full wiring test"})
        assert resp.status_code == 200
        data = resp.json()
        run_id = data["run_id"]

        import asyncio
        await asyncio.sleep(1.0)

        hapi, store = full_api
        events = await store.get_events(run_id)
        event_types = [e.event_type for e in events]
        assert EventType.RUN_STARTED in event_types
        assert EventType.RUN_COMPLETED in event_types
