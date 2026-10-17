"""TDD: WebSocket auth uses the same JWT (finding I1 / auth findings)."""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from harness.api.app import HarnessAPI, app, get_hapi
from harness.api.auth.jwt_provider import JwtAuthProvider
from harness.models.events import EventType, RunStartedPayload
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from tests.support.jwt import make_settings, mint_token


@pytest.fixture
def jwt_keys(tmp_path):
    from tests.support.jwt import generate_rsa_keypair

    return generate_rsa_keypair(tmp_path)


@pytest.fixture
def ws_api(jwt_keys):
    store = EventStore(":memory:")
    asyncio.run(store.initialize())
    hapi = HarnessAPI(store=store, executor=ToolExecutor(store))
    app.dependency_overrides[get_hapi] = lambda: hapi
    previous = getattr(app.state, "auth_provider", None)
    _, public_dir, kid = jwt_keys
    app.state.auth_provider = JwtAuthProvider(make_settings(public_dir))
    yield store, jwt_keys, kid
    app.dependency_overrides.clear()
    app.state.auth_provider = previous
    asyncio.run(store.close())


def _token(jwt_keys, *, tenant: str, subject: str = "user:alice") -> str:
    private_pem, _, kid = jwt_keys
    return mint_token(private_pem, kid, subject=subject, tenant_id=tenant)


def test_ws_valid_jwt_receives_events(ws_api):
    store, jwt_keys, _ = ws_api
    asyncio.run(
        store.append_event(
            "ws-run",
            EventType.RUN,
            RunStartedPayload(intent="ws auth").model_dump(),
            tenant_id="tenant-a",
        )
    )
    token = _token(jwt_keys, tenant="tenant-a")
    client = TestClient(app)
    with client.websocket_connect(
        "/api/v1/runs/ws-run/events",
        subprotocols=["jagent.jwt", f"jagent.token.{token}"],
    ) as ws:
        assert ws.accepted_subprotocol == "jagent.jwt"
        event = ws.receive_json()
        assert event["event_type"] == "Run"
        assert event["tenant_id"] == "tenant-a"


def test_ws_without_token_is_rejected(ws_api):
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/runs/ws-run/events"):
            pass
    assert exc.value.code == 4401


def test_ws_cross_tenant_is_rejected(ws_api):
    store, jwt_keys, _ = ws_api
    asyncio.run(
        store.append_event(
            "ws-run-b",
            EventType.RUN,
            RunStartedPayload(intent="secret").model_dump(),
            tenant_id="tenant-b",
        )
    )
    token_a = _token(jwt_keys, tenant="tenant-a")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/api/v1/runs/ws-run-b/events",
            subprotocols=["jagent.jwt", f"jagent.token.{token_a}"],
        ):
            pass
    assert exc.value.code == 4404
