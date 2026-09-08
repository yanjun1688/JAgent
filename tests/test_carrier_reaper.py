from __future__ import annotations

import pytest

from harness.execution.docker import DockerSandboxBackend
from harness.execution.reaper import plan_carrier_reaping, reap_orphaned_carriers
from harness.models.events import EventType
from harness.storage.event_store import EventStore


# ── Docker identity labels ─────────────────────────────────────


def test_docker_run_args_include_identity_labels_when_run_bound(tmp_path):
    backend = DockerSandboxBackend("img", str(tmp_path), "/workspace", run_id="run-abc", tenant_id="acme")
    args = backend._docker_run_args()
    # The container must be self-describing so a restarted process can find and
    # reap it after a crash (cross-process discovery, like browser profile locks).
    assert "--label" in args
    assert "harness.managed=1" in args
    assert "harness.run_id=run-abc" in args
    # tenant label is diagnostic only; sanitized to docker-label charset.
    assert any(a.startswith("harness.tenant_id=") for a in args)


def test_docker_run_args_without_run_binding_are_unmanaged(tmp_path):
    # Defensive: a backend with no run identity must not claim to be managed.
    backend = DockerSandboxBackend("img", str(tmp_path), "/workspace")
    args = backend._docker_run_args()
    assert "harness.managed=1" not in args


# ── Reaper selection (pure) ────────────────────────────────────


def test_plan_carrier_reaping_reaps_only_managed_terminal_containers():
    containers = [
        {"id": "c-dead", "run_id": "r-dead"},       # run terminal -> reap
        {"id": "c-alive", "run_id": "r-alive"},     # run still active -> keep
        {"id": "c-vanished", "run_id": "r-gone"},   # no events / unknown -> reap
        {"id": "c-foreign", "run_id": None},        # unlabeled, not ours -> never touch
    ]
    active = {"r-alive"}

    reap = plan_carrier_reaping(containers, active)

    assert reap == ["c-dead", "c-vanished"]
    assert "c-alive" not in reap
    assert "c-foreign" not in reap


# ── Reaper integration (fake docker CLI) ───────────────────────


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_reap_orphaned_carriers_removes_only_dead_run_containers(store, monkeypatch):
    # r-dead: terminal (FAILED) — its container is a leak after a crash.
    await store.append_event("r-dead", EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}})
    await store.append_event("r-dead", EventType.RUN_FAILED, {"final_error": "boom", "event_count": 2})
    # r-alive: still RUNNING — its container must be left alone.
    await store.append_event("r-alive", EventType.RUN_STARTED, {"intent": "y", "context_snapshot": {}})

    fake_containers = [
        {"id": "c-dead", "run_id": "r-dead"},
        {"id": "c-alive", "run_id": "r-alive"},
        {"id": "c-foreign", "run_id": None},
    ]
    removed: list[str] = []

    async def fake_list():
        return fake_containers

    async def fake_remove(cid):
        removed.append(cid)

    monkeypatch.setattr("harness.execution.reaper.docker_available", lambda: True)
    monkeypatch.setattr("harness.execution.reaper.list_managed_containers", fake_list)
    monkeypatch.setattr("harness.execution.reaper.remove_container", fake_remove)

    count = await reap_orphaned_carriers(store)

    assert count == 1
    assert removed == ["c-dead"]


@pytest.mark.asyncio
async def test_reap_orphaned_carriers_noop_without_docker(store, monkeypatch):
    monkeypatch.setattr("harness.execution.reaper.docker_available", lambda: False)

    listed = False

    async def fake_list():
        nonlocal listed
        listed = True
        return []

    monkeypatch.setattr("harness.execution.reaper.list_managed_containers", fake_list)
    count = await reap_orphaned_carriers(store)
    assert count == 0
    assert listed is False
