"""TDD: workspace execution root must be bound to the authenticated tenant.

Reproduces pentest findings #2/#4/#7/#19/#34/#44:
an admin can currently set filesystem_root to the shared base dir or another
tenant's directory, then use file_op to read/write/delete other tenants' files.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from harness.api.app import HarnessAPI, app, get_hapi
from harness.core.tenant import reset_current_principal, safe_tenant_dir_component, set_current_principal
from harness.execution.boundary import WORKSPACE_BASE_DIR, WorkspaceRootError
from harness.execution.factory import create_backend
from harness.models.principal import Principal, Role
from harness.models.workspace import ExecutionTarget, ExecutionTargetType, Workspace, WorkspaceScope
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from tests.support.auth import auth_headers

BASE = WORKSPACE_BASE_DIR


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
async def client(api):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def directory_scope(root: str) -> dict:
    return {"target": {"type": "directory", "filesystem_root": root}}


def sandbox_scope(host_mount_src: str) -> dict:
    return {
        "target": {
            "type": "sandbox",
            "docker_image": "busybox",
            "host_mount_src": host_mount_src,
            "mount_root": "/workspace",
        }
    }


def remote_scope(key_path: str) -> dict:
    return {
        "target": {
            "type": "remote",
            "host": "example.com",
            "username": "ops",
            "private_key_path": key_path,
            "remote_root": "/workspace",
        }
    }


async def _create_workspace(client: AsyncClient, tenant: str, root: str, name: str) -> dict:
    response = await client.post(
        "/api/v1/workspaces",
        headers=auth_headers(tenant),
        json={"name": name, "scope": directory_scope(root)},
    )
    return {"status": response.status_code, "body": response.json()}


async def test_base_dir_itself_is_rejected(client):
    result = await _create_workspace(client, "tenant-a", str(BASE), "base-root")
    assert result["status"] == 422, result


async def test_other_tenant_directory_is_rejected(client):
    other = BASE / "tenant-b" / "work"
    result = await _create_workspace(client, "tenant-a", str(other), "other-tenant")
    assert result["status"] == 422, result


async def test_tenant_dir_itself_is_rejected(client):
    result = await _create_workspace(client, "tenant-a", str(BASE / "tenant-a"), "tenant-root")
    assert result["status"] == 422, result


async def test_own_tenant_subdirectory_is_accepted(client):
    own = BASE / "tenant-a" / "work"
    result = await _create_workspace(client, "tenant-a", str(own), "own-tenant")
    assert result["status"] == 201, result


async def test_sandbox_host_mount_is_bound_to_tenant(client):
    other = BASE / "tenant-b" / "work"
    response = await client.post(
        "/api/v1/workspaces",
        headers=auth_headers("tenant-a"),
        json={"name": "sb-other", "scope": sandbox_scope(str(other))},
    )
    assert response.status_code == 422, response.text

    own = BASE / "tenant-a" / "sandbox"
    response = await client.post(
        "/api/v1/workspaces",
        headers=auth_headers("tenant-a"),
        json={"name": "sb-own", "scope": sandbox_scope(str(own))},
    )
    assert response.status_code == 201, response.text


async def test_remote_key_is_bound_to_tenant_key_directory(client, tmp_path, monkeypatch):
    import harness.execution.boundary as boundary

    monkeypatch.setattr(boundary, "REMOTE_KEY_BASE_DIR", tmp_path)

    own = tmp_path / "tenant-a" / "id_rsa"
    own.parent.mkdir(parents=True)
    own.write_text("private key", encoding="utf-8")
    other = tmp_path / "tenant-b" / "id_rsa"
    other.parent.mkdir(parents=True)
    other.write_text("private key", encoding="utf-8")

    accepted = await client.post(
        "/api/v1/workspaces",
        headers=auth_headers("tenant-a"),
        json={"name": "remote-own", "scope": remote_scope(str(own))},
    )
    assert accepted.status_code == 201, accepted.text

    outside = await client.post(
        "/api/v1/workspaces",
        headers=auth_headers("tenant-a"),
        json={"name": "remote-other", "scope": remote_scope(str(other))},
    )
    assert outside.status_code == 422, outside.text

    missing = tmp_path / "tenant-a" / "missing"
    absent = await client.post(
        "/api/v1/workspaces",
        headers=auth_headers("tenant-a"),
        json={"name": "remote-missing", "scope": remote_scope(str(missing))},
    )
    assert absent.status_code == 422, absent.text


async def test_update_cannot_move_root_to_another_tenant(client):
    own = BASE / "tenant-a" / "work"
    created = await client.post(
        "/api/v1/workspaces",
        headers=auth_headers("tenant-a"),
        json={"name": "update-me", "scope": directory_scope(str(own))},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["workspace_id"]

    other = BASE / "tenant-b" / "work"
    updated = await client.patch(
        f"/api/v1/workspaces/{workspace_id}",
        headers=auth_headers("tenant-a"),
        json={"scope": directory_scope(str(other))},
    )
    assert updated.status_code == 422, updated.text


async def test_preexisting_malicious_workspace_is_rejected_at_run_start(api, client):
    _, store = api
    now = time.time()
    malicious = Workspace(
        workspace_id="ws_malicious",
        tenant_id="tenant-a",
        name="malicious",
        scope=WorkspaceScope(
            target=ExecutionTarget(
                type=ExecutionTargetType.DIRECTORY,
                filesystem_root=str(BASE),
            )
        ),
        created_at=now,
        updated_at=now,
    )
    await store.create_workspace(malicious)

    response = await client.post(
        "/api/v1/runs",
        headers=auth_headers("tenant-a", "operator"),
        json={"intent": "read other tenants", "workspace_id": "ws_malicious"},
    )
    assert response.status_code == 422, response.text


async def test_create_backend_rejects_base_root():
    target = ExecutionTarget(
        type=ExecutionTargetType.DIRECTORY,
        filesystem_root=str(BASE),
    )
    with pytest.raises(WorkspaceRootError):
        await create_backend(target, tenant_id="tenant-a")


async def test_create_backend_falls_back_to_request_principal():
    target = ExecutionTarget(
        type=ExecutionTargetType.DIRECTORY,
        filesystem_root=str(BASE),
    )
    token = set_current_principal(
        Principal(
            subject="test:tenant-a",
            tenant_id="tenant-a",
            roles={Role.ADMIN},
            auth_method="test",
        )
    )
    try:
        with pytest.raises(WorkspaceRootError):
            await create_backend(target)
    finally:
        reset_current_principal(token)


def test_tenant_dir_component_is_injective():
    assert safe_tenant_dir_component("tenant") == "tenant"
    assert safe_tenant_dir_component("acme") != safe_tenant_dir_component("acme.")
    assert safe_tenant_dir_component(".") != safe_tenant_dir_component("..")
    assert safe_tenant_dir_component("ten ant") != safe_tenant_dir_component("ten_ant")
    assert len(safe_tenant_dir_component("x" * 128)) <= 64
