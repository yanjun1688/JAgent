"""Backend integration tests across HTTP, storage, folding, and analysis.

The tests use the real FastAPI routes, dependency injection, ScopedEventStore,
EventStore, fold logic, and AnalysisService. External LLM, Docker, SSH, MCP,
and browser services remain out of scope and are not replaced by fake HTTP
responses in this suite.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from harness.api.app import HarnessAPI, app, get_hapi
from harness.models.events import (
    AgentThoughtPayload,
    EventType,
    GuardrailTriggeredPayload,
    RunCompletedPayload,
    RunStartedPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


@pytest.fixture
async def backend():
    store = EventStore(":memory:")
    await store.initialize()
    api = HarnessAPI(store=store, executor=ToolExecutor(store))
    app.dependency_overrides[get_hapi] = lambda: api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield api, store, client
    app.dependency_overrides.clear()
    await store.close()


def directory_scope(root: str = "D:/Project/JAgent/data/workspaces/integration/work") -> dict:
    return {"target": {"type": "directory", "filesystem_root": root}}


async def seed_analysable_run(store: EventStore, run_id: str = "integration-run") -> None:
    await store.append_event(run_id, EventType.RUN_STARTED, RunStartedPayload(intent="integration").model_dump())
    await store.append_event(
        run_id, EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="observe", token_count=7).model_dump()
    )
    await store.append_event(
        run_id,
        EventType.TOOL_CALLED,
        ToolCalledPayload(tool_call_id="tc-1", tool_name="echo", input={"value": "ok"}).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        ToolCompletedPayload(tool_call_id="tc-1", tool_name="echo", output={"ok": True}, duration_ms=12).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.GUARDRAIL_TRIGGERED,
        GuardrailTriggeredPayload(
            tool_call_id="tc-1", tool_name="echo", guardrail_id="qa", reason="test trigger"
        ).model_dump(),
    )
    await store.append_event(run_id, EventType.RUN_COMPLETED, RunCompletedPayload(result_summary="done").model_dump())


class TestWorkspaceRunConversationIntegration:
    async def test_workspace_crud_and_audit_events_through_http(self, backend):
        _, store, client = backend
        created = await client.post(
            "/api/v1/workspaces",
            json={"name": "integration", "description": "before", "scope": directory_scope()},
        )
        assert created.status_code == 201
        workspace_id = created.json()["workspace_id"]

        updated = await client.patch(
            f"/api/v1/workspaces/{workspace_id}",
            json={"description": "after"},
        )
        assert updated.status_code == 200
        assert updated.json()["description"] == "after"

        listed = await client.get("/api/v1/workspaces")
        assert listed.status_code == 200
        assert any(item["workspace_id"] == workspace_id for item in listed.json()["workspaces"])

        events = await client.get(f"/api/v1/workspaces/{workspace_id}/events")
        assert events.status_code == 200
        assert [event["event_type"] for event in events.json()["events"]] == ["WorkspaceCreated", "WorkspaceUpdated"]

        deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"success": True}
        assert (await store.get_workspace(workspace_id)).status == "deleted"

    async def test_run_http_lifecycle_is_folded_from_persisted_events(self, backend):
        _, store, client = backend
        created = await client.post("/api/v1/runs", json={"intent": "pause me"})
        assert created.status_code == 200
        run_id = created.json()["run_id"]

        paused = await client.post(f"/api/v1/runs/{run_id}/pause", json={"reason": "integration"})
        assert paused.status_code == 200
        resumed = await client.post(f"/api/v1/runs/{run_id}/resume")
        assert resumed.status_code == 200

        await store.append_event(
            run_id, EventType.RUN_COMPLETED, RunCompletedPayload(result_summary="done").model_dump()
        )
        detail = await client.get(f"/api/v1/runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"
        assert detail.json()["event_count"] == 4

        events = await client.get(f"/api/v1/runs/{run_id}/events?from_seq=2&limit=10")
        assert events.status_code == 200
        assert [event["seq"] for event in events.json()["events"]] == [2, 3, 4]

    async def test_conversation_message_creates_run_and_message_event(self, backend):
        _, store, client = backend
        conversation = await client.post("/api/v1/conversations", json={"title": "integration chat"})
        assert conversation.status_code == 201
        conversation_id = conversation.json()["conversation_id"]

        sent = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "hello", "client_request_id": "request-1"},
        )
        assert sent.status_code == 200
        assert sent.json()["claimed"] is True
        run_id = sent.json()["run_id"]

        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        assert detail.status_code == 200
        assert detail.json()["conversation"]["message_count"] == 1
        assert detail.json()["messages"][0]["content"] == "hello"
        assert await store.get_events(run_id)

        duplicate = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "hello", "client_request_id": "request-1"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["claimed"] is False
        assert duplicate.json()["run_id"] == run_id


class TestAnalysisQueryIntegration:
    async def test_analysis_http_endpoints_share_event_store_projection(self, backend):
        _, store, client = backend
        await seed_analysable_run(store)

        expected = {
            "/api/v1/analysis/dashboard": "overview",
            "/api/v1/analysis/tools": "tools",
            "/api/v1/analysis/guardrails": "guardrails",
            "/api/v1/analysis/runs/integration-run": "run_id",
            "/api/v1/analysis/runs/integration-run/timeline": "timeline",
            "/api/v1/analysis/runs/integration-run/tool-traces": "tool_traces",
        }
        for path, field in expected.items():
            response = await client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert field in response.json()

        dashboard = await client.get("/api/v1/analysis/dashboard")
        assert dashboard.json()["overview"]["total_runs"] == 1
        traces = await client.get("/api/v1/analysis/runs/integration-run/tool-traces")
        assert traces.json()["tool_traces"][0]["tool_name"] == "echo"

    @pytest.mark.parametrize(
        "query_type",
        [
            "runs",
            "run",
            "events",
            "dashboard",
            "tool-stats",
            "guardrail-stats",
            "run-analysis",
            "timeline",
            "tool-traces",
            "tool-defs",
            "schedulers",
            "mcp",
            "plans",
            "system",
            "ws-clients",
            "feedback",
            "monitor",
            "health",
        ],
    )
    async def test_unified_query_dispatches_every_declared_type(self, backend, query_type):
        api, store, client = backend
        await seed_analysable_run(store)
        needs_run_id = {"run", "events", "run-analysis", "timeline", "tool-traces", "plans"}
        params = {"type": query_type}
        if query_type in needs_run_id:
            params["run_id"] = "integration-run"
        response = await client.get("/api/v1/query", params=params)
        assert response.status_code == 200, (query_type, response.text)
        assert response.json()["type"] == query_type

    async def test_operations_retry_is_explicitly_not_implemented(self, backend):
        _, _, client = backend
        response = await client.post("/api/v1/operations/retry")
        assert response.status_code == 501
        assert response.json()["error"] == "Not Implemented"


class TestTenantAndWebSocketIntegration:
    async def test_empty_tenant_header_falls_back_to_default(self, backend):
        """Bug A 回归（P1-13 13.3）：空 `X-Tenant-Id: ""` 必须回退 default，
        不得触发 `set_current_tenant("")` 的 ValueError → 500。"""
        _, store, client = backend
        response = await client.post(
            "/api/v1/runs",
            headers={"X-Tenant-Id": ""},
            json={"intent": "list"},
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        events = await store.get_events(run_id)
        assert events and events[0].tenant_id == "default"

    async def test_blank_tenant_header_falls_back_to_default(self, backend):
        _, _, client = backend
        response = await client.post(
            "/api/v1/runs",
            headers={"X-Tenant-Id": "   "},
            json={"intent": "list"},
        )
        assert response.status_code == 200, response.text

    async def test_tenant_header_isolation_applies_to_http_storage(self, backend):
        _, _, client = backend
        created = await client.post(
            "/api/v1/workspaces",
            headers={"X-Tenant-Id": "tenant-a"},
            json={"name": "same-name", "scope": directory_scope()},
        )
        assert created.status_code == 201
        workspace_id = created.json()["workspace_id"]

        hidden = await client.get(f"/api/v1/workspaces/{workspace_id}", headers={"X-Tenant-Id": "tenant-b"})
        assert hidden.status_code == 404
        visible = await client.get(f"/api/v1/workspaces/{workspace_id}", headers={"X-Tenant-Id": "tenant-a"})
        assert visible.status_code == 200

    async def test_websocket_broadcast_crosses_event_store_callback(self, backend):
        api, store, _ = backend
        received: list[str] = []

        class FakeWebSocket:
            async def send_text(self, event_json: str) -> None:
                received.append(event_json)

        api._ws_clients["ws-run"] = [FakeWebSocket()]
        api.wire_broadcast()
        await store.append_event("ws-run", EventType.RUN_STARTED, RunStartedPayload(intent="ws").model_dump())
        await asyncio.sleep(0)
        assert len(received) == 1
        assert json.loads(received[0])["event_type"] == "RunStarted"
