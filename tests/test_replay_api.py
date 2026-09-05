"""API + boundary tests for the Event Replay Inspector (read-only GET API).

Covers:
  - meta / timeline / state / diff happy paths
  - 404 for unknown run, 400 for out-of-range / inverted seq
  - multi-tenant isolation (another tenant's run reads as 404)
  - read-only static guard: replay surface exposes only GET and the replay
    package imports no write/execution component
  - Langfuse link field is reserved (null) unless a provider is injected
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from httpx import ASGITransport, AsyncClient

from harness.api.app import HarnessAPI, app, get_hapi
from harness.models.events import EventType, RunStartedPayload
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor

from .replay_fixtures import seed_failed_plan_run


# -- Fixtures (mirrors tests/test_api.py) --


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


# -- Meta --


class TestReplayMeta:
    async def test_meta_returns_status_and_counts(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/meta")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run-1"
        assert data["status"] == "failed"
        assert data["latest_seq"] == 11
        assert data["event_count"] == 11
        assert data["langfuse_trace_url"] is None  # reserved, not wired this release

    async def test_meta_unknown_run_is_404(self, client):
        resp = await client.get("/api/v1/replay/runs/nope/meta")
        assert resp.status_code == 404
        assert resp.json()["error"] == "Run not found"


# -- Timeline --


class TestReplayTimeline:
    async def test_timeline_paginates(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/timeline?cursor=0&limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 11
        assert len(data["timeline"]) == 5
        assert data["has_more"] is True
        assert data["next_cursor"] == 5
        assert data["timeline"][0]["seq"] == 1

        resp2 = await client.get("/api/v1/replay/runs/run-1/timeline?cursor=10&limit=5")
        data2 = resp2.json()
        assert [e["seq"] for e in data2["timeline"]] == [11]
        assert data2["has_more"] is False

    async def test_timeline_flags_terminal_events(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/timeline?cursor=10&limit=5")
        last = resp.json()["timeline"][0]
        assert last["event_type"] == "RunFailed"
        assert last["is_terminal"] is True

    async def test_timeline_unknown_run_is_404(self, client):
        resp = await client.get("/api/v1/replay/runs/nope/timeline")
        assert resp.status_code == 404


# -- State at a point --


class TestReplayState:
    async def test_state_at_latest_is_failed(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert data["is_latest"] is True
        assert data["at_seq"] == 11
        assert len(data["guardrail_blocks"]) == 1
        assert data["guardrail_blocks"][0]["guardrail_id"] == "no_write_outside_workspace"

    async def test_state_at_midpoint_is_historical_running(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/state?at_seq=6")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["is_latest"] is False
        steps = {s["step_id"]: s["status"] for s in data["plan"]["steps"]}
        assert steps["s1"] == "completed"
        assert steps["s2"] == "pending"
        # The future guardrail block must not leak into the historical view.
        assert data["guardrail_blocks"] == []

    async def test_state_unknown_run_is_404(self, client):
        resp = await client.get("/api/v1/replay/runs/nope/state")
        assert resp.status_code == 404

    async def test_state_seq_beyond_latest_is_400(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/state?at_seq=999")
        assert resp.status_code == 400
        assert "out of range" in resp.json()["error"]

    async def test_state_seq_below_one_is_rejected_by_validation(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/state?at_seq=0")
        assert resp.status_code == 422  # Query(ge=1)


# -- Diff --


class TestReplayDiff:
    async def test_diff_highlights_status_transition_and_failed_step(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/diff?from_seq=6&to_seq=11")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status_change"] == {"from_status": "running", "to_status": "failed"}
        changed = {c["step_id"]: c for c in data["steps_changed"]}
        assert changed["s2"]["to_status"] == "failed"
        assert "s1" not in changed
        assert any(g["guardrail_id"] == "no_write_outside_workspace" for g in data["guardrails_triggered"])
        assert [e["seq"] for e in data["events_in_range"]] == [7, 8, 9, 10, 11]

    async def test_diff_inverted_range_is_400(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/diff?from_seq=11&to_seq=6")
        assert resp.status_code == 400

    async def test_diff_out_of_range_is_400(self, client, api):
        _, store = api
        await seed_failed_plan_run(store, "run-1")
        resp = await client.get("/api/v1/replay/runs/run-1/diff?from_seq=1&to_seq=999")
        assert resp.status_code == 400

    async def test_diff_unknown_run_is_404(self, client):
        resp = await client.get("/api/v1/replay/runs/nope/diff?from_seq=1&to_seq=2")
        assert resp.status_code == 404


# -- Multi-tenant isolation --


class TestReplayTenantIsolation:
    async def test_other_tenant_run_is_invisible(self, client, api):
        _, store = api
        # Run belongs to tenant-a; the default-tenant client must not see it.
        await seed_failed_plan_run(store, "secret-run", tenant_id="tenant-a")

        resp = await client.get("/api/v1/replay/runs/secret-run/meta")
        assert resp.status_code == 404
        resp = await client.get("/api/v1/replay/runs/secret-run/state")
        assert resp.status_code == 404

        # The owning tenant can read it.
        ok = await client.get(
            "/api/v1/replay/runs/secret-run/meta", headers={"X-Tenant-Id": "tenant-a"}
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "failed"

    async def test_partial_visible_stream_does_not_500_on_early_seq(self, client, api):
        # Regression: a run_id shared across tenants where the current tenant
        # only owns a high-seq event. The visible stream starts at seq 21; a
        # request for an earlier point must be a clean 400, not a fold-on-empty
        # 500 (root cause: bounds validated against [1, latest] not the visible
        # stream's actual first seq).
        _, store = api
        from harness.models.events import AgentThoughtPayload, RunStartedPayload

        for seq in range(1, 21):
            etype = EventType.RUN_STARTED if seq == 1 else EventType.AGENT_THOUGHT
            payload = (
                RunStartedPayload(intent="tenant-a run").model_dump()
                if seq == 1
                else AgentThoughtPayload(thought=f"t{seq}").model_dump()
            )
            await store.append_event("split-run", etype, payload, tenant_id="tenant-a")
        # One event owned by the *default* tenant -> seq 21, the only visible one.
        await store.append_event(
            "split-run", EventType.RUN_STARTED, RunStartedPayload(intent="default slice").model_dump()
        )

        # Latest visible state folds fine.
        ok = await client.get("/api/v1/replay/runs/split-run/state")
        assert ok.status_code == 200
        assert ok.json()["at_seq"] == 21

        # A point before the first visible seq is a 400 (previously 500).
        early = await client.get("/api/v1/replay/runs/split-run/state?at_seq=10")
        assert early.status_code == 400

        # A diff window starting before the first visible seq is a 400.
        bad_diff = await client.get("/api/v1/replay/runs/split-run/diff?from_seq=1&to_seq=21")
        assert bad_diff.status_code == 400


# -- Read-only static guard --


FORBIDDEN_IMPORT_ROOTS = (
    "harness.core.scheduler",
    "harness.core.dag_executor",
    "harness.core.planner",
    "harness.core.agent_kernel",
    "harness.core.llm_client",
    "harness.core.context_manager",
    "harness.core.lifecycle",
    "harness.core.contract_extractor",
    "harness.tools",
    "harness.execution",
    "harness.monitoring",
)


def _iter_imports(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_replay_package_imports_are_read_only():
    # Given the replay package + router sources
    pkg = pathlib.Path(__file__).resolve().parent.parent / "harness" / "replay"
    files = list(pkg.glob("*.py")) + [
        pathlib.Path(__file__).resolve().parent.parent / "harness" / "api" / "replay_routes.py"
    ]
    assert files, "replay package must exist"
    # When scanning all their imports
    offenders = []
    for f in files:
        for mod in _iter_imports(f):
            if any(mod == root or mod.startswith(root + ".") for root in FORBIDDEN_IMPORT_ROOTS):
                offenders.append(f"{f.name}: {mod}")
    # Then no write/execution/monitoring component is imported
    assert not offenders, f"read-only replay must not import write/execution modules: {offenders}"


def test_replay_router_exposes_only_get_routes():
    # Given the replay router is registered on the app
    replay_paths = [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/replay")]
    assert replay_paths, "replay routes must be registered"
    # Then every route only allows GET (no write verbs)
    for route in replay_paths:
        methods = {m for m in route.methods if m not in ("HEAD", "OPTIONS")}
        assert methods == {"GET"}, f"{route.path} must be GET-only, got {methods}"


async def test_empty_run_with_only_run_started_is_still_inspectable(client, api):
    # Given a run that has a RunStarted event but nothing else
    _, store = api
    await store.append_event(
        "empty-run", EventType.RUN_STARTED, RunStartedPayload(intent="just started").model_dump()
    )
    # When fetching its state
    resp = await client.get("/api/v1/replay/runs/empty-run/state")
    # Then it returns a valid running state (no crash on missing plan/results)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert data["plan"] is None
    assert data["tool_results"] == []
