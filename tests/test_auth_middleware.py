"""TDD: AuthMiddleware + object-level authorization (P1/P2).

Reproduces the pentest auth findings:
- #1/#6  unauthenticated control-plane access
- #14/#18/#41/#46  X-Tenant-Id impersonation / tenant enumeration
- #15/#30/#31/#32/#35/#38/#39/#45/#48  cross-tenant pause/resume
- #25  unauthenticated / forged operator confirmation
- #36  forged operator_id persisted into the event stream
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harness.api.app import HarnessAPI, app, get_hapi
from harness.api.auth.jwt_provider import JwtAuthProvider
from harness.api.auth.middleware import DEFAULT_POLICY
from harness.models.events import (
    ConfirmationRequestedPayload,
    EventType,
    RunPausedPayload,
    RunStartedPayload,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from tests.support.jwt import generate_rsa_keypair, make_settings, mint_token


@pytest.fixture
def jwt_keys(tmp_path):
    return generate_rsa_keypair(tmp_path)


@pytest.fixture
async def api():
    store = EventStore(":memory:")
    await store.initialize()
    hapi = HarnessAPI(store=store, executor=ToolExecutor(store))
    app.dependency_overrides[get_hapi] = lambda: hapi
    yield hapi, store
    app.dependency_overrides.clear()
    await store.close()


@pytest.fixture
def jwt_provider(jwt_keys):
    private_pem, public_dir, kid = jwt_keys
    previous = getattr(app.state, "auth_provider", None)
    app.state.auth_provider = JwtAuthProvider(make_settings(public_dir))
    yield private_pem, kid
    app.state.auth_provider = previous


@pytest.fixture
async def client(api, jwt_provider):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(jwt_provider, *, tenant: str = "default", subject: str = "user:alice", roles=("admin",)) -> str:
    private_pem, kid = jwt_provider
    return mint_token(private_pem, kid, subject=subject, tenant_id=tenant, roles=list(roles))


def directory_scope(root: str) -> dict:
    return {"target": {"type": "directory", "filesystem_root": root}}


# ── Authentication ───────────────────────────────────────────────


async def test_no_token_is_rejected_on_read(client):
    response = await client.get("/api/v1/workspaces")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_no_token_is_rejected_before_body_validation(client):
    # A malformed body must still be 401, not FastAPI's 422: authentication
    # happens in the middleware before routing/body parsing.
    response = await client.post(
        "/api/v1/runs",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


async def test_invalid_token_is_rejected(client):
    response = await client.get("/api/v1/workspaces", headers={"Authorization": "Bearer garbage"})
    assert response.status_code == 401


async def test_x_tenant_id_alone_does_not_authenticate(client):
    response = await client.get("/api/v1/workspaces", headers={"X-Tenant-Id": "tenant-a"})
    assert response.status_code == 401


async def test_x_tenant_id_cannot_override_jwt_tenant(client, jwt_provider):
    token_a = token_for(jwt_provider, tenant="tenant-a")
    created = await client.post(
        "/api/v1/workspaces",
        headers=bearer(token_a),
        json={"name": "tenant-a-ws", "scope": directory_scope("data/workspaces/tenant-a/work")},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["workspace_id"]

    token_b = token_for(jwt_provider, tenant="tenant-b")
    spoofed = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers={**bearer(token_b), "X-Tenant-Id": "tenant-a"},
    )
    assert spoofed.status_code == 404

    owner = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=bearer(token_a))
    assert owner.status_code == 200


async def test_health_is_public(client):
    response = await client.get("/health")
    assert response.status_code == 200


async def test_whoami_returns_principal(client, jwt_provider):
    token = token_for(jwt_provider, tenant="tenant-a", subject="ops:alice", roles=("operator",))
    response = await client.get("/api/v1/auth/whoami", headers=bearer(token))
    assert response.status_code == 200
    data = response.json()
    assert data["subject"] == "ops:alice"
    assert data["tenant_id"] == "tenant-a"
    assert data["roles"] == ["operator"]


# ── Role policy ──────────────────────────────────────────────────


async def test_viewer_cannot_create_run(client, jwt_provider):
    token = token_for(jwt_provider, roles=("viewer",))
    response = await client.post("/api/v1/runs", headers=bearer(token), json={"intent": "x"})
    assert response.status_code == 403


async def test_operator_can_create_run(client, jwt_provider):
    token = token_for(jwt_provider, roles=("operator",))
    response = await client.post("/api/v1/runs", headers=bearer(token), json={"intent": "x"})
    assert response.status_code == 200


async def test_operator_cannot_manage_workspaces(client, jwt_provider):
    token = token_for(jwt_provider, roles=("operator",))
    response = await client.post(
        "/api/v1/workspaces",
        headers=bearer(token),
        json={"name": "nope", "scope": directory_scope("data/workspaces/tenant-a/work")},
    )
    assert response.status_code == 403


async def test_admin_can_manage_workspaces(client, jwt_provider):
    token = token_for(jwt_provider, roles=("admin",))
    response = await client.post(
        "/api/v1/workspaces",
        headers=bearer(token),
        json={"name": "yes", "scope": directory_scope("data/workspaces/default/work")},
    )
    assert response.status_code == 201


async def test_every_api_route_has_a_policy():
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1"):
            continue
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            sample = path
            for part in path.split("/"):
                if part.startswith("{") and part.endswith("}"):
                    sample = sample.replace(part, "sample-id")
            assert DEFAULT_POLICY.required_role(method, sample) is not None, (method, path)


# ── Object-level authorization (cross-tenant) ────────────────────


async def _seed_run(store: EventStore, run_id: str, tenant_id: str) -> None:
    await store.append_event(
        run_id,
        EventType.RUN,
        RunStartedPayload(intent="cross-tenant").model_dump(),
        tenant_id=tenant_id,
    )


@pytest.mark.parametrize("action", ["pause", "resume", "feedback"])
async def test_cross_tenant_run_control_is_404(client, jwt_provider, api, action):
    _, store = api
    await _seed_run(store, "run-b", "tenant-b")
    token_a = token_for(jwt_provider, tenant="tenant-a")
    body = {"text": "hi", "priority": "low"} if action == "feedback" else {}
    response = await client.post(f"/api/v1/runs/run-b/{action}", headers=bearer(token_a), json=body)
    assert response.status_code == 404


async def test_cross_tenant_delete_is_404(client, jwt_provider, api):
    _, store = api
    await _seed_run(store, "run-b", "tenant-b")
    token_a = token_for(jwt_provider, tenant="tenant-a")
    response = await client.delete("/api/v1/runs/run-b", headers=bearer(token_a))
    assert response.status_code == 404


async def test_cross_tenant_confirm_is_404(client, jwt_provider, api):
    _, store = api
    await _seed_confirmable_run(store, "run-b", "tenant-b")
    token_a = token_for(jwt_provider, tenant="tenant-a")
    response = await client.post(
        "/api/v1/runs/run-b/confirm",
        headers=bearer(token_a),
        json={"confirmation_id": "cid-b", "confirmed": True, "operator_id": "forged"},
    )
    assert response.status_code == 404


async def test_cross_tenant_events_are_404(client, jwt_provider, api):
    _, store = api
    await _seed_run(store, "run-b", "tenant-b")
    token_a = token_for(jwt_provider, tenant="tenant-a")
    response = await client.get("/api/v1/runs/run-b/events", headers=bearer(token_a))
    assert response.status_code == 404


async def _seed_confirmable_run(store: EventStore, run_id: str, tenant_id: str) -> None:
    await store.append_event(
        run_id,
        EventType.RUN,
        RunStartedPayload(intent="confirm me").model_dump(),
        tenant_id=tenant_id,
    )
    await store.append_event(
        run_id,
        EventType.CONFIRMATION,
        ConfirmationRequestedPayload(
            confirmation_id="cid-b",
            tool_call_id="tc-b",
            tool_name="file_op",
            input={"operation": "delete", "path": "x"},
            idempotency_key="ik-b",
        ).model_dump(),
        tenant_id=tenant_id,
    )
    await store.append_event(
        run_id,
        EventType.RUN,
        RunPausedPayload(reason="waiting for confirmation").model_dump(),
        tenant_id=tenant_id,
    )


async def test_confirm_operator_id_comes_from_principal_not_body(client, jwt_provider, api):
    _, store = api
    await _seed_confirmable_run(store, "run-b", "tenant-b")
    token_b = token_for(
        jwt_provider,
        tenant="tenant-b",
        subject="ops:bob",
        roles=("operator",),
    )
    response = await client.post(
        "/api/v1/runs/run-b/confirm",
        headers=bearer(token_b),
        json={"confirmation_id": "cid-b", "confirmed": True, "operator_id": "root-admin@victim"},
    )
    assert response.status_code == 200, response.text

    events = await store.get_events("run-b")
    received = [
        event
        for event in events
        if event.event_type == EventType.CONFIRMATION and event.phase == "received"
    ]
    assert len(received) == 1
    assert received[0].payload["operator_id"] == "ops:bob"


async def test_cross_tenant_run_is_not_listed(client, jwt_provider, api):
    _, store = api
    await _seed_run(store, "run-b", "tenant-b")
    token_a = token_for(jwt_provider, tenant="tenant-a")
    response = await client.get("/api/v1/runs", headers=bearer(token_a))
    assert response.status_code == 200
    assert response.json()["total"] == 0
