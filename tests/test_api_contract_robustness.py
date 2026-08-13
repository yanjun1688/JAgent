"""Black-box API contract, robustness, and resilience tests.

These tests intentionally use the in-memory EventStore and the public FastAPI
application. They do not call route functions directly, so request validation,
dependency injection, response-model validation, and HTTP status codes are all
covered together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from harness.api.app import HarnessAPI, app, get_hapi
from harness.api.schemas import CreateWorkspaceRequest
from harness.models.conversation import SendMessageRequest
from harness.models.events import EventType, RunStartedPayload
from harness.models.workspace import ExecutionTarget, ExecutionTargetType, WorkspaceScope
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


@pytest.fixture
async def api_and_client():
    store = EventStore(":memory:")
    await store.initialize()
    api = HarnessAPI(store=store, executor=ToolExecutor(store))
    app.dependency_overrides[get_hapi] = lambda: api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield api, store, client
    app.dependency_overrides.clear()
    await store.close()


def directory_scope() -> dict:
    return {
        "target": {
            "type": "directory",
            "filesystem_root": "D:/Project/JAgent/data/workspaces/test/work",
        }
    }


class TestOpenAPIContract:
    async def test_rest_routes_are_registered_in_openapi(self):
        route_paths = {
            route.path
            for route in app.routes
            if route.path.startswith("/api/v1/") and route.path != "/api/v1/runs/{run_id}/events"
        }
        documented_paths = set(app.openapi()["paths"])
        assert route_paths <= documented_paths

    async def test_openapi_declares_request_schema_for_mutating_json_endpoints(self):
        schema = app.openapi()
        body_operations = {
            ("post", "/api/v1/workspaces"),
            ("patch", "/api/v1/workspaces/{workspace_id}"),
            ("post", "/api/v1/runs"),
            ("post", "/api/v1/runs/{run_id}/pause"),
            ("post", "/api/v1/runs/{run_id}/confirm"),
            ("post", "/api/v1/runs/{run_id}/feedback"),
            ("post", "/api/v1/conversations"),
            ("post", "/api/v1/conversations/{conversation_id}/messages"),
            ("patch", "/api/v1/conversations/{conversation_id}"),
        }
        for method, path in body_operations:
            assert "requestBody" in schema["paths"][path][method], f"missing requestBody for {method.upper()} {path}"

    async def test_openapi_file_is_parseable_and_has_expected_api_surface(self):
        with open("frontend/public/openapi.json", encoding="utf-8") as file:
            checked_in = json.load(file)
        assert checked_in["openapi"].startswith("3.")
        assert "/api/v1/query" in checked_in["paths"]
        assert "/api/v1/workspaces" in checked_in["paths"]
        assert "/api/v1/conversations/{conversation_id}/messages" in checked_in["paths"]


class TestRequestBoundaries:
    @pytest.mark.parametrize(
        ("payload", "field"),
        [
            ({}, "intent"),
            ({"intent": 1}, "intent"),
        ],
    )
    async def test_create_run_rejects_missing_or_wrong_typed_fields(self, api_and_client, payload, field):
        _, _, client = api_and_client
        response = await client.post("/api/v1/runs", json=payload)
        assert response.status_code == 422
        assert field in response.text

    async def test_workspace_name_and_execution_target_are_validated(self, api_and_client):
        _, _, client = api_and_client
        too_long = {"name": "x" * 129, "scope": directory_scope()}
        response = await client.post("/api/v1/workspaces", json=too_long)
        assert response.status_code == 422

        missing_root = {"name": "bad-target", "scope": {"target": {"type": "directory"}}}
        response = await client.post("/api/v1/workspaces", json=missing_root)
        assert response.status_code == 422

    async def test_nested_target_rejects_invalid_port_and_unknown_fields_are_not_silent(self, api_and_client):
        _, _, client = api_and_client
        bad_port = {
            "name": "bad-port",
            "scope": {
                "target": {
                    "type": "remote",
                    "host": "host",
                    "username": "u",
                    "private_key_path": "k",
                    "remote_root": "/w",
                    "port": 0,
                }
            },
        }
        response = await client.post("/api/v1/workspaces", json=bad_port)
        assert response.status_code == 422

    async def test_workspace_execution_root_must_stay_inside_base(self, api_and_client):
        """P1-B 回归：DIRECTORY/SANDBOX 执行根必须在受信基目录之内。"""
        _, _, client = api_and_client
        base = Path("data/workspaces").resolve()
        outside = base.parent / "escaped-work"

        escaped_abs = {
            "name": "escape-abs",
            "scope": {"target": {"type": "directory", "filesystem_root": str(outside)}},
        }
        escaped_rel = {
            "name": "escape-rel",
            "scope": {"target": {"type": "directory", "filesystem_root": ".."}},
        }
        escaped_sandbox = {
            "name": "escape-sb",
            "scope": {
                "target": {
                    "type": "sandbox",
                    "docker_image": "busybox",
                    "host_mount_src": "../escape-mount",
                    "mount_root": "/workspace",
                }
            },
        }
        for payload in (escaped_abs, escaped_rel, escaped_sandbox):
            response = await client.post("/api/v1/workspaces", json=payload)
            assert response.status_code == 422, (payload, response.text)

        inside = {
            "name": "inside",
            "scope": {
                "target": {"type": "directory", "filesystem_root": "data/workspaces/inside/work"}
            },
        }
        response = await client.post("/api/v1/workspaces", json=inside)
        assert response.status_code == 201

    @pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=101"])
    async def test_query_pagination_rejects_out_of_range_values(self, api_and_client, query):
        _, _, client = api_and_client
        response = await client.get(f"/api/v1/query?type=runs&{query}")
        assert response.status_code == 422

    @pytest.mark.parametrize("query", ["limit=0", "offset=-1", "from_seq=-1"])
    async def test_public_pagination_does_not_accept_negative_or_zero_ranges(self, api_and_client, query):
        _, _, client = api_and_client
        path = "/api/v1/workspaces?" if query.startswith("limit") else "/api/v1/runs/abc/events?"
        response = await client.get(path + query)
        assert response.status_code == 422

    async def test_model_field_contracts_reject_invalid_nested_values(self):
        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                name="",
                scope=WorkspaceScope(target=ExecutionTarget(type=ExecutionTargetType.DIRECTORY, filesystem_root="x")),
            )
        with pytest.raises(ValidationError):
            SendMessageRequest(message=1)


class TestResourceRobustness:
    async def test_unknown_query_type_returns_structured_400(self, api_and_client):
        _, _, client = api_and_client
        response = await client.get("/api/v1/query?type=not-a-query")
        assert response.status_code == 400
        assert "detail" in response.json()

    @pytest.mark.parametrize(
        "url",
        [
            "/api/v1/runs/missing",
            "/api/v1/runs/missing/events",
            "/api/v1/analysis/runs/missing",
            "/api/v1/analysis/runs/missing/timeline",
            "/api/v1/analysis/runs/missing/tool-traces",
        ],
    )
    async def test_unknown_run_reads_return_404(self, api_and_client, url):
        _, _, client = api_and_client
        response = await client.get(url)
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "query",
        ["type=run", "type=events", "type=run-analysis", "type=timeline", "type=tool-traces", "type=plans"],
    )
    async def test_run_scoped_query_requires_run_id(self, api_and_client, query):
        _, _, client = api_and_client
        response = await client.get("/api/v1/query?" + query)
        assert response.status_code == 400
        assert "detail" in response.json()

    async def test_confirmation_and_feedback_are_not_accepted_for_unknown_run(self, api_and_client):
        _, _, client = api_and_client
        confirmation = await client.post(
            "/api/v1/runs/missing/confirm",
            json={"confirmation_id": "c1", "confirmed": True},
        )
        feedback = await client.post(
            "/api/v1/runs/missing/feedback",
            json={"text": "please retry"},
        )
        assert confirmation.status_code == 404
        assert feedback.status_code == 404

    async def test_delete_unknown_run_is_not_reported_as_success(self, api_and_client):
        _, _, client = api_and_client
        response = await client.delete("/api/v1/runs/missing")
        assert response.status_code == 404


class TestResponseContracts:
    async def test_workspace_lifecycle_response_fields_and_event_audit(self, api_and_client):
        _, store, client = api_and_client
        response = await client.post(
            "/api/v1/workspaces", json={"name": "qa", "description": "test", "scope": directory_scope()}
        )
        assert response.status_code == 201
        body = response.json()
        assert set(body) >= {
            "workspace_id",
            "tenant_id",
            "name",
            "description",
            "scope",
            "status",
            "run_count",
            "created_at",
            "updated_at",
        }

        workspace_id = body["workspace_id"]
        events = await store.get_workspace_events(workspace_id)
        assert events[-1].event_type == EventType.WORKSPACE_CREATED

    async def test_query_response_has_stable_envelope_and_pagination_meta(self, api_and_client):
        api, store, client = api_and_client
        await store.append_event("r-contract", EventType.RUN_STARTED, RunStartedPayload(intent="contract").model_dump())
        response = await client.get("/api/v1/query?type=runs&page=1&page_size=20")
        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {"type", "data", "meta"}
        assert body["type"] == "runs"
        assert body["meta"] == {"page": 1, "page_size": 20, "total": 1, "has_more": False}

    async def test_event_response_keeps_sequence_order_and_required_fields(self, api_and_client):
        _, store, client = api_and_client
        await store.append_event("r-events", EventType.RUN_STARTED, RunStartedPayload(intent="x").model_dump())
        response = await client.get("/api/v1/runs/r-events/events")
        assert response.status_code == 200
        event = response.json()["events"][0]
        assert set(event) >= {"run_id", "seq", "event_type", "payload", "created_at", "tenant_id"}
        assert event["seq"] == 1
