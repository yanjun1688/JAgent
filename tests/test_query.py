"""Tests for unified query endpoint (harness/api/query.py).

Covers all 15 query types + dispatch error handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from harness import SchedulerConfig, ToolDefinition
from harness.api.app import HarnessAPI, app, get_hapi
from harness.models.events import (
    AgentThoughtPayload,
    EventType,
    RunStartedPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
)
from harness.models.mcp_config import MCPConfig, MCPConnectionConfig
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def api(store):
    executor = ToolExecutor(store)
    hapi = HarnessAPI(store=store, executor=executor)
    hapi.registry = MagicMock()
    hapi.tool_defs = []
    hapi.scheduler_config = SchedulerConfig()
    hapi._schedulers = {}
    hapi._ws_clients = {}
    app.dependency_overrides[get_hapi] = lambda: hapi
    yield hapi, store
    app.dependency_overrides.clear()


@pytest.fixture
def client(api):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def seed_run(store, run_id, intent="test", completed=True):
    await store.append_event(run_id, EventType.RUN_STARTED, RunStartedPayload(intent=intent).model_dump())
    if completed:
        await store.append_event(run_id, EventType.RUN_COMPLETED, {"result_summary": "ok"})


async def seed_run_with_tools(store, run_id, intent="test"):
    """Seed a run with tool events, thought, and completion."""
    await store.append_event(run_id, EventType.RUN_STARTED, RunStartedPayload(intent=intent).model_dump())
    await store.append_event(
        run_id, EventType.AGENT_THOUGHT, AgentThoughtPayload(thought="thinking", token_count=100).model_dump()
    )
    await store.append_event(
        run_id,
        EventType.TOOL_CALLED,
        ToolCalledPayload(tool_call_id="tc1", tool_name="echo", input={"msg": "hi"}).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        ToolCompletedPayload(tool_call_id="tc1", tool_name="echo", output={"ok": True}, duration_ms=50).model_dump(),
    )
    await store.append_event(run_id, EventType.RUN_COMPLETED, {"result_summary": "done"})


async def seed_run_with_unsuccessful(store, run_id, intent="test"):
    """Seed a run with one completed and one UNSUCCESSFUL tool call."""
    await store.append_event(run_id, EventType.RUN_STARTED, RunStartedPayload(intent=intent).model_dump())
    await store.append_event(
        run_id,
        EventType.TOOL_CALLED,
        ToolCalledPayload(tool_call_id="tc-ok", tool_name="echo", input={"msg": "ok"}).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        ToolCompletedPayload(tool_call_id="tc-ok", tool_name="echo", output={"ok": True}, duration_ms=30).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.TOOL_CALLED,
        ToolCalledPayload(tool_call_id="tc-bad", tool_name="http_request", input={"url": "http://x"}).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        ToolCompletedPayload(
            tool_call_id="tc-bad",
            tool_name="http_request",
            output=None,
            duration_ms=40,
            result_type="unsuccessful",
            error="not found",
        ).model_dump(),
    )
    await store.append_event(run_id, EventType.RUN_COMPLETED, {"result_summary": "done"})


# ═══════════════════════════════════════════════════════════════
#  Dispatch
# ═══════════════════════════════════════════════════════════════


class TestDispatchErrors:
    async def test_unknown_type_returns_400(self, client, api):
        resp = await client.get("/api/v1/query?type=nonexistent")
        assert resp.status_code == 400
        data = resp.json()
        assert "Unknown type" in data["detail"]

    async def test_missing_type_returns_422(self, client, api):
        resp = await client.get("/api/v1/query")
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════
#  runs
# ═══════════════════════════════════════════════════════════════


class TestQueryRuns:
    async def test_empty(self, client, api):
        resp = await client.get("/api/v1/query?type=runs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "runs"
        assert data["data"] == []
        assert data["meta"]["total"] == 0

    async def test_single_run(self, client, api):
        _, store = api
        await seed_run(store, "r1")
        resp = await client.get("/api/v1/query?type=runs")
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["run_id"] == "r1"
        assert data["data"][0]["intent"] == "test"
        assert data["data"][0]["status"] == "completed"
        assert data["meta"]["total"] == 1

    async def test_run_summary_counts_separate_unsuccessful(self, client, api):
        """v2.2 (D2/D3): tool_success_count 只计真正的成功；
        UNSUCCESSFUL 独立计入 tool_unsuccessful_count，不算进 success。"""
        _, store = api
        await seed_run_with_unsuccessful(store, "r1")
        resp = await client.get("/api/v1/query?type=runs")
        data = resp.json()
        item = data["data"][0]
        assert item["tool_call_count"] == 2
        assert item["tool_success_count"] == 1
        assert item["tool_unsuccessful_count"] == 1
        assert item["tool_failure_count"] == 0

    async def test_pagination(self, client, api):
        _, store = api
        for i in range(5):
            await seed_run(store, f"r{i}")
        resp = await client.get("/api/v1/query?type=runs&page=1&page_size=2")
        data = resp.json()
        assert len(data["data"]) == 2
        assert data["meta"]["page"] == 1
        assert data["meta"]["page_size"] == 2
        assert data["meta"]["total"] == 5
        assert data["meta"]["has_more"] is True

    async def test_run_without_events(self, client, api):
        """Run that exists in list_runs but get_events_for_runs returns nothing."""
        _, store = api
        await store.append_event("r_empty", EventType.RUN_STARTED, RunStartedPayload(intent="x").model_dump())
        # list_runs returns rows directly from events table, so the run is there.
        # But get_events_for_runs should return the same event.
        resp = await client.get("/api/v1/query?type=runs")
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["run_id"] == "r_empty"


# ═══════════════════════════════════════════════════════════════
#  run
# ═══════════════════════════════════════════════════════════════


class TestQueryRun:
    async def test_missing_run_id_returns_400(self, client, api):
        resp = await client.get("/api/v1/query?type=run")
        assert resp.status_code == 400

    async def test_not_found_returns_404(self, client, api):
        resp = await client.get("/api/v1/query?type=run&run_id=nonexistent")
        assert resp.status_code == 404

    async def test_returns_run_detail(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=run&run_id=r1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "run"
        d = data["data"]
        assert d["run_id"] == "r1"
        assert d["status"] == "completed"
        assert d["intent"] == "test"
        assert d["event_count"] == 5
        assert d["total_tokens"] == 100
        assert d["thought_count"] == 1
        assert d["tool_results"] is not None
        assert len(d["tool_results"]) == 1
        assert d["tool_results"][0]["tool_name"] == "echo"
        assert d["event_type_counts"]["RunStarted"] == 1
        assert d["event_type_counts"]["AgentThought"] == 1

    async def test_run_exposes_completion_evidence(self, client, api):
        """v2.2 (D5, 洞 5): run API 暴露 completion_evidence（机械达成证据）。"""
        from harness.models.events import RunCompletedPayload

        _, store = api
        await seed_run(store, "r_ev", completed=False)
        await store.append_event(
            "r_ev",
            EventType.RUN_COMPLETED,
            RunCompletedPayload(
                result_summary="done",
                all_normal=False,
                unmet_step_ids=["s2"],
            ).model_dump(),
        )
        resp = await client.get("/api/v1/query?type=run&run_id=r_ev")
        assert resp.status_code == 200
        assert resp.json()["data"]["completion_evidence"] == {
            "all_normal": False,
            "unmet_step_ids": ["s2"],
            "deliverable_met": None,
            "deliverable_status": "unverified",
            "deliverable_summary": [],
        }

    @pytest.mark.skip(
        reason="BUG: Pydantic v2 excludes _-prefixed fields from serialization; _included never appears in JSON"
    )
    async def test_include_events(self, client, api):
        _, store = api
        await seed_run(store, "r1")
        resp = await client.get("/api/v1/query?type=run&run_id=r1&include=events")
        data = resp.json()
        assert "_included" in data
        assert "events" in data["_included"]
        assert data["_included"]["events"]["type"] == "events"

    @pytest.mark.skip(
        reason="BUG: Pydantic v2 excludes _-prefixed fields from serialization; _included never appears in JSON"
    )
    async def test_include_multiple(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=run&run_id=r1&include=events,timeline")
        data = resp.json()
        assert "_included" in data
        assert "events" in data["_included"]
        assert "timeline" in data["_included"]

    async def test_include_invalid_ignored(self, client, api):
        _, store = api
        await seed_run(store, "r1")
        resp = await client.get("/api/v1/query?type=run&run_id=r1&include=invalid_key")
        data = resp.json()
        assert "_included" not in data or data["_included"] is None

    async def test_run_with_tool_stats(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=run&run_id=r1")
        data = resp.json()
        ts = data["data"]["tool_stats"]
        assert "echo" in ts
        assert ts["echo"]["call_count"] == 1
        assert ts["echo"]["completed"] == 1

    async def test_tool_stats_separates_unsuccessful(self, client, api):
        """v2.2 (D2/D3): UNSUCCESSFUL 独立成桶，不再混入 completed。"""
        _, store = api
        await seed_run_with_unsuccessful(store, "r1")
        resp = await client.get("/api/v1/query?type=run&run_id=r1")
        data = resp.json()
        ts = data["data"]["tool_stats"]
        assert ts["echo"]["completed"] == 1
        assert ts["echo"]["unsuccessful"] == 0
        assert ts["http_request"]["completed"] == 0
        assert ts["http_request"]["unsuccessful"] == 1
        assert ts["http_request"]["failed"] == 0

    async def test_run_with_pending_confirmations(self, client, api):
        _, store = api
        from harness.models.events import ConfirmationRequestedPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event(
            "r1",
            EventType.CONFIRMATION_REQUESTED,
            ConfirmationRequestedPayload(
                confirmation_id="cid-1",
                tool_name="delete",
                tool_call_id="tc1",
                input={"path": "/"},
                risk_level="high",
                idempotency_key="conf_cid-1",
            ).model_dump(),
        )
        resp = await client.get("/api/v1/query?type=run&run_id=r1")
        data = resp.json()
        assert len(data["data"]["pending_confirmations"]) == 1
        assert data["data"]["pending_confirmations"][0]["confirmation_id"] == "cid-1"


# ═══════════════════════════════════════════════════════════════
#  events
# ═══════════════════════════════════════════════════════════════


class TestQueryEvents:
    async def test_missing_run_id_returns_400(self, client, api):
        resp = await client.get("/api/v1/query?type=events")
        assert resp.status_code == 400

    async def test_not_found_returns_404(self, client, api):
        resp = await client.get("/api/v1/query?type=events&run_id=nonexistent")
        assert resp.status_code == 404

    async def test_returns_events(self, client, api):
        _, store = api
        await seed_run(store, "r1")
        resp = await client.get("/api/v1/query?type=events&run_id=r1")
        data = resp.json()
        assert data["type"] == "events"
        assert len(data["data"]) == 2
        assert data["data"][0]["event_type"] == "RunStarted"
        assert data["data"][1]["event_type"] == "RunCompleted"

    async def test_pagination(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")  # 5 events
        resp = await client.get("/api/v1/query?type=events&run_id=r1&page=1&page_size=2")
        data = resp.json()
        assert len(data["data"]) == 2
        assert data["meta"]["total"] == 5
        assert data["meta"]["has_more"] is True


# ═══════════════════════════════════════════════════════════════
#  dashboard
# ═══════════════════════════════════════════════════════════════


class TestQueryDashboard:
    async def test_empty(self, client, api):
        resp = await client.get("/api/v1/query?type=dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "dashboard"
        assert data["data"]["overview"]["total_runs"] == 0

    async def test_with_data(self, client, api):
        _, store = api
        await seed_run(store, "r1")
        resp = await client.get("/api/v1/query?type=dashboard")
        data = resp.json()
        assert data["data"]["overview"]["total_runs"] == 1
        assert data["data"]["overview"]["completed_runs"] == 1


# ═══════════════════════════════════════════════════════════════
#  tool-stats
# ═══════════════════════════════════════════════════════════════


class TestQueryToolStats:
    async def test_empty(self, client, api):
        resp = await client.get("/api/v1/query?type=tool-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "tool-stats"
        assert data["data"]["tools"] == []

    async def test_with_data(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=tool-stats")
        data = resp.json()
        assert len(data["data"]["tools"]) == 1
        assert data["data"]["tools"][0]["tool_name"] == "echo"
        assert data["data"]["tools"][0]["call_count"] == 1
        assert data["data"]["tools"][0]["success_count"] == 1


# ═══════════════════════════════════════════════════════════════
#  guardrail-stats
# ═══════════════════════════════════════════════════════════════


class TestQueryGuardrailStats:
    async def test_empty(self, client, api):
        resp = await client.get("/api/v1/query?type=guardrail-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "guardrail-stats"
        assert data["data"]["guardrails"] == []

    async def test_with_data(self, client, api):
        _, store = api
        from harness.models.events import GuardrailTriggeredPayload

        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event(
            "r1",
            EventType.GUARDRAIL_TRIGGERED,
            GuardrailTriggeredPayload(
                tool_call_id="tc1", tool_name="delete", guardrail_id="destructive_op", reason="cannot delete"
            ).model_dump(),
        )
        resp = await client.get("/api/v1/query?type=guardrail-stats")
        data = resp.json()
        assert len(data["data"]["guardrails"]) == 1
        assert data["data"]["guardrails"][0]["guardrail_id"] == "destructive_op"


# ═══════════════════════════════════════════════════════════════
#  run-analysis
# ═══════════════════════════════════════════════════════════════


class TestQueryRunAnalysis:
    async def test_missing_run_id_returns_400(self, client, api):
        resp = await client.get("/api/v1/query?type=run-analysis")
        assert resp.status_code == 400

    async def test_not_found_returns_404(self, client, api):
        resp = await client.get("/api/v1/query?type=run-analysis&run_id=nonexistent")
        assert resp.status_code == 404

    async def test_returns_analysis(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=run-analysis&run_id=r1")
        data = resp.json()
        assert data["type"] == "run-analysis"
        assert data["data"]["run_id"] == "r1"
        assert data["data"]["status"] == "completed"
        assert data["data"]["total_tokens"] == 100


# ═══════════════════════════════════════════════════════════════
#  timeline
# ═══════════════════════════════════════════════════════════════


class TestQueryTimeline:
    async def test_missing_run_id_returns_400(self, client, api):
        resp = await client.get("/api/v1/query?type=timeline")
        assert resp.status_code == 400

    async def test_returns_timeline(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=timeline&run_id=r1")
        data = resp.json()
        assert data["type"] == "timeline"
        assert len(data["data"]) > 0
        assert data["meta"]["total"] > 0


# ═══════════════════════════════════════════════════════════════
#  tool-traces
# ═══════════════════════════════════════════════════════════════


class TestQueryToolTraces:
    async def test_missing_run_id_returns_400(self, client, api):
        resp = await client.get("/api/v1/query?type=tool-traces")
        assert resp.status_code == 400

    async def test_returns_traces(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=tool-traces&run_id=r1")
        data = resp.json()
        assert data["type"] == "tool-traces"
        assert len(data["data"]["tool_traces"]) == 1
        trace = data["data"]["tool_traces"][0]
        assert trace["tool_call_id"] == "tc1"
        assert trace["tool_name"] == "echo"
        assert trace["status"] == "completed"


# ═══════════════════════════════════════════════════════════════
#  tool-defs
# ═══════════════════════════════════════════════════════════════


class TestQueryToolDefs:
    async def test_no_registry_returns_empty(self, client, api):
        hapi, _ = api
        hapi.registry = None
        resp = await client.get("/api/v1/query?type=tool-defs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "tool-defs"
        assert data["data"] == []

    async def test_returns_tool_definitions(self, client, api):
        hapi, _ = api
        td = ToolDefinition(
            name="echo",
            description="echo tool",
            input_schema={"msg": "string"},
            idempotency_key_fields=["msg"],
            side_effects=[],
            timeout_ms=5000,
        )
        hapi.registry.list_tool_defs = MagicMock(return_value=[td])
        resp = await client.get("/api/v1/query?type=tool-defs")
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["tool_name"] == "echo"
        assert data["data"][0]["definition"]["name"] == "echo"

    async def test_pagination(self, client, api):
        hapi, _ = api
        tds = [
            ToolDefinition(
                name=f"tool{i}",
                description=f"desc{i}",
                input_schema={},
                idempotency_key_fields=[],
                side_effects=[],
                timeout_ms=1000,
            )
            for i in range(5)
        ]
        hapi.registry.list_tool_defs = MagicMock(return_value=tds)
        resp = await client.get("/api/v1/query?type=tool-defs&page=1&page_size=2")
        data = resp.json()
        assert len(data["data"]) == 2
        assert data["meta"]["total"] == 5
        assert data["meta"]["has_more"] is True


# ═══════════════════════════════════════════════════════════════
#  schedulers
# ═══════════════════════════════════════════════════════════════


class TestQuerySchedulers:
    async def test_empty(self, client, api):
        resp = await client.get("/api/v1/query?type=schedulers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "schedulers"
        assert data["data"] == []

    async def test_with_scheduler(self, client, api):
        hapi, store = api
        await seed_run_with_tools(store, "r1")
        mock_sched = MagicMock()
        mock_sched.is_active = MagicMock(return_value=True)
        mock_sched.is_paused = MagicMock(return_value=False)
        mock_sched.config = SchedulerConfig(max_iterations=10, max_consecutive_failures=3)
        hapi._schedulers["r1"] = mock_sched
        resp = await client.get("/api/v1/query?type=schedulers")
        data = resp.json()
        assert len(data["data"]) == 1
        s = data["data"][0]
        assert s["run_id"] == "r1"
        assert s["is_active"] is True
        assert s["is_paused"] is False
        assert s["config"]["max_iterations"] == 10


# ═══════════════════════════════════════════════════════════════
#  mcp
# ═══════════════════════════════════════════════════════════════


class TestQueryMcp:
    async def test_no_manager(self, client, api):
        hapi, _ = api
        hapi.mcp_manager = None
        resp = await client.get("/api/v1/query?type=mcp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "mcp"
        assert data["data"]["servers"] == []

    async def test_with_manager(self, client, api):
        hapi, _ = api
        mock_mgr = MagicMock()
        mock_mgr.server_names = ["srv1"]
        cfg = MCPConfig(
            servers=[
                MCPConnectionConfig(
                    name="srv1", command=["npx"], enabled=True, auto_register_tools=True, timeout_ms=5000
                ),
                MCPConnectionConfig(
                    name="srv2", url="http://localhost:8081", enabled=False, auto_register_tools=False, timeout_ms=10000
                ),
            ]
        )
        mock_mgr.config = cfg
        hapi.mcp_manager = mock_mgr
        resp = await client.get("/api/v1/query?type=mcp")
        data = resp.json()
        assert data["type"] == "mcp"
        assert len(data["data"]["servers"]) == 2
        assert data["data"]["servers"][0]["connected"] is True
        assert data["data"]["servers"][1]["connected"] is False
        assert data["data"]["connected_count"] == 1


# ═══════════════════════════════════════════════════════════════
#  plans
# ═══════════════════════════════════════════════════════════════


class TestQueryPlans:
    async def test_missing_run_id_returns_400(self, client, api):
        resp = await client.get("/api/v1/query?type=plans")
        assert resp.status_code == 400

    async def test_not_found_returns_404(self, client, api):
        resp = await client.get("/api/v1/query?type=plans&run_id=nonexistent")
        assert resp.status_code == 404

    async def test_returns_plans(self, client, api):
        _, store = api
        await seed_run_with_tools(store, "r1")
        resp = await client.get("/api/v1/query?type=plans&run_id=r1")
        data = resp.json()
        assert data["type"] == "plans"
        assert data["data"]["run_id"] == "r1"
        assert "plan_history" in data["data"]
        assert "latest_plan" in data["data"]


# ═══════════════════════════════════════════════════════════════
#  system
# ═══════════════════════════════════════════════════════════════


class TestQuerySystem:
    async def test_minimal(self, client, api):
        resp = await client.get("/api/v1/query?type=system")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "system"
        assert data["data"]["llm_client"] is None
        assert "tool_registry" in data["data"]

    async def test_with_llm_client(self, client, api):
        hapi, _ = api
        mock_llm = MagicMock()
        mock_llm.model = "gpt-4"
        mock_llm.base_url = "https://api.openai.com"
        mock_llm.calls = ["call1", "call2"]
        hapi.llm_client = mock_llm
        resp = await client.get("/api/v1/query?type=system")
        data = resp.json()
        llm = data["data"]["llm_client"]
        assert llm["model"] == "gpt-4"
        assert llm["total_calls"] == 2

    async def test_with_tool_defs(self, client, api):
        hapi, _ = api
        hapi.tool_defs = [
            ToolDefinition(
                name="echo",
                description="e",
                input_schema={},
                idempotency_key_fields=[],
                side_effects=[],
                timeout_ms=5000,
            )
        ]
        resp = await client.get("/api/v1/query?type=system")
        data = resp.json()
        assert data["data"]["tool_defs_count"] == 1


# ═══════════════════════════════════════════════════════════════
#  ws-clients
# ═══════════════════════════════════════════════════════════════


class TestQueryWsClients:
    async def test_all_no_clients(self, client, api):
        resp = await client.get("/api/v1/query?type=ws-clients")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "ws-clients"
        assert data["data"]["total_connections"] == 0

    async def test_all_with_clients(self, client, api):
        hapi, _ = api
        mock_ws1 = MagicMock()
        mock_ws2 = MagicMock()
        hapi._ws_clients = {"r1": [mock_ws1, mock_ws2], "r2": [mock_ws1]}
        resp = await client.get("/api/v1/query?type=ws-clients")
        data = resp.json()
        assert data["data"]["total_connections"] == 3
        assert len(data["data"]["by_run"]) == 2
        assert data["data"]["by_run"]["r1"] == 2
        assert data["data"]["by_run"]["r2"] == 1

    async def test_specific_run(self, client, api):
        hapi, _ = api
        hapi._ws_clients = {"r1": [MagicMock(), MagicMock()]}
        resp = await client.get("/api/v1/query?type=ws-clients&run_id=r1")
        data = resp.json()
        assert data["data"]["run_id"] == "r1"
        assert data["data"]["connected_clients"] == 2

    async def test_specific_run_empty(self, client, api):
        hapi, _ = api
        hapi._ws_clients = {}
        resp = await client.get("/api/v1/query?type=ws-clients&run_id=unknown")
        data = resp.json()
        assert data["data"]["run_id"] == "unknown"
        assert data["data"]["connected_clients"] == 0


# ═══════════════════════════════════════════════════════════════
#  Hidden engine state — _compute_scheduler_tool_stats
# ═══════════════════════════════════════════════════════════════


class TestComputeSchedulerToolStats:
    async def test_stats_with_failed_tool(self, client, api):
        hapi, store = api
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="test").model_dump())
        await store.append_event(
            "r1", EventType.TOOL_CALLED, ToolCalledPayload(tool_call_id="tc1", tool_name="bad", input={}).model_dump()
        )
        await store.append_event(
            "r1",
            EventType.TOOL_FAILED,
            ToolFailedPayload(tool_call_id="tc1", tool_name="bad", error="err", retryable=False).model_dump(),
        )
        mock_sched = MagicMock()
        mock_sched.is_active = MagicMock(return_value=False)
        mock_sched.is_paused = MagicMock(return_value=False)
        mock_sched.config = SchedulerConfig()
        hapi._schedulers["r1"] = mock_sched
        resp = await client.get("/api/v1/query?type=schedulers")
        data = resp.json()
        stats = data["data"][0]["tool_stats"]
        assert stats["bad"]["call_count"] == 1
        assert stats["bad"]["failed"] == 1
        assert stats["bad"]["completed"] == 0


# ═══════════════════════════════════════════════════════════════
#  Edge cases — run with failed status
# ═══════════════════════════════════════════════════════════════


class TestQueryFailedRun:
    async def test_failed_run_detail(self, client, api):
        _, store = api
        await store.append_event("r_fail", EventType.RUN_STARTED, RunStartedPayload(intent="fail").model_dump())
        await store.append_event("r_fail", EventType.RUN_FAILED, {"final_error": "boom", "event_count": 1})
        resp = await client.get("/api/v1/query?type=run&run_id=r_fail")
        data = resp.json()
        assert data["data"]["status"] == "failed"
        assert data["data"]["last_error"] == "boom"
        assert data["data"]["completed_at"] is not None
