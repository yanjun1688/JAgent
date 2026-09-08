"""ADR-011 §3.2/§3.4 integration tests — BrowserPool / BrowserLease with a mocked
playwright-mcp stdio process.

No real Chrome or node subprocess is required: ``bp.stdio_client`` and
``bp.ClientSession`` are faked, while the pool's own spawn / lock / routing
logic runs for real. Cross-platform green on Linux CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.models.browser import BrowserConfig, BrowserMode
from harness.models.events import (
    EventType,
    RunCompletedPayload,
    RunFailedPayload,
    RunOrphanedPayload,
)
from harness.tools.browser_pool import (
    BrowserLeaseUnavailableError,
    BrowserPool,
)

BROWSER_TOOL_NAMES = [
    "browser_close",
    "browser_console_messages",
    "browser_click",
    "browser_drag",
    "browser_drop",
    "browser_evaluate",
    "browser_file_upload",
    "browser_fill_form",
    "browser_find",
    "browser_handle_dialog",
    "browser_hover",
    "browser_navigate",
    "browser_navigate_back",
    "browser_network_request",
    "browser_network_requests",
    "browser_press_key",
    "browser_resize",
    "browser_run_code_unsafe",
    "browser_select_option",
    "browser_snapshot",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_type",
    "browser_wait_for",
]


def _tool(name: str, desc: str = "desc") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=desc, inputSchema={"type": "object"})


def _result_ok(texts: list[str] | None = None) -> MagicMock:
    result = MagicMock()
    result.isError = False
    items = []
    for t in texts or ["ok"]:
        item = MagicMock()
        item.text = t
        item.data = None
        items.append(item)
    result.content = items
    return result


@pytest.fixture
def mcp_fake(monkeypatch):
    """Replace the process boundary with in-memory fakes.

    Exercises the real ``BrowserPool._spawn_lease`` flow (command build, output
    dir, profile lock write, session init/list_tools) without a subprocess.
    """
    import harness.tools.browser_pool as bp

    state = SimpleNamespace(
        tool_names=list(BROWSER_TOOL_NAMES),
        sessions=[],
        transports=[],
        session_exits=0,
        transport_exits=0,
    )

    class _SessionCM:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            session = AsyncMock()
            session.initialize = AsyncMock(return_value=None)
            session.list_tools = AsyncMock(return_value=MagicMock(tools=[_tool(n) for n in state.tool_names]))
            session.call_tool = AsyncMock(return_value=_result_ok())
            session.close = AsyncMock()
            self.session = session
            state.sessions.append(session)
            return session

        async def __aexit__(self, exc_type, exc, tb):
            state.session_exits += 1
            return False

    class _TransportCM:
        def __init__(self):
            self.exited = False

        async def __aenter__(self):
            return (AsyncMock(), AsyncMock())

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True
            state.transport_exits += 1
            return False

    def _fake_stdio_client(params):
        cm = _TransportCM()
        state.transports.append(cm)
        return cm

    monkeypatch.setattr(bp, "resolve_playwright_mcp_command", lambda explicit=None: ["fake-pw-mcp"])
    monkeypatch.setattr(bp, "stdio_client", _fake_stdio_client)
    monkeypatch.setattr(bp, "ClientSession", _SessionCM)
    return state


def make_config(tmp_path, mode: BrowserMode = BrowserMode.ISOLATED, **overrides) -> BrowserConfig:
    params: dict = {
        "mode": mode,
        "profile_root": tmp_path / "profiles",
        "output_root": tmp_path / "out",
        "headless": True,
        "lease_wait_ms": 5000,
    }
    params.update(overrides)
    return BrowserConfig(**params)


def _lock_file_for(tmp_path, tenant: str, workspace: str):
    pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT))
    return pool._prepare_profile(tenant, workspace)[1]


class TestAcquireRelease:
    async def test_lazy_acquire_spawns_one_process_per_run(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path))
        assert pool.get("r1") is None

        lease = await pool.acquire("r1", tenant_id="t1", workspace_id="w1")
        assert lease.run_id == "r1"
        assert lease.tool_names == BROWSER_TOOL_NAMES
        assert len(mcp_fake.sessions) == 1

        # Different run → a different process/session (isolation).
        lease2 = await pool.acquire("r2", tenant_id="t1", workspace_id="w1")
        assert lease2 is not lease
        assert len(mcp_fake.sessions) == 2

    async def test_same_run_reuses_lease_without_new_spawn(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path))
        first = await pool.acquire("r1", tenant_id="t", workspace_id="w")
        second = await pool.acquire("r1", tenant_id="t", workspace_id="w")
        assert second is first
        assert len(mcp_fake.sessions) == 1
        assert pool.get("r1") is first

    async def test_release_removes_lease_and_closes_session(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path))
        await pool.acquire("r1", tenant_id="t", workspace_id="w")
        await pool.release("r1")
        assert pool.get("r1") is None
        assert mcp_fake.session_exits == 1
        assert mcp_fake.transport_exits == 1
        # Release of an unknown run is a no-op.
        await pool.release("ghost")
        assert mcp_fake.session_exits == 1

    async def test_shutdown_releases_all_runs(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path))
        await pool.acquire("r1", tenant_id="t", workspace_id="w")
        await pool.acquire("r2", tenant_id="t", workspace_id="w")
        await pool.shutdown()
        assert pool.get("r1") is None
        assert pool.get("r2") is None
        assert mcp_fake.session_exits == 2
        assert mcp_fake.transport_exits == 2

    async def test_output_dir_created_under_tenant_run(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path))
        await pool.acquire("run-42", tenant_id="Tenant One", workspace_id="ws")
        out_root = (tmp_path / "out" / "Tenant_One" / "run-42").resolve()
        assert out_root.exists()


class TestLeaseSerialization:
    async def test_calls_within_one_lease_are_serialized(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path))
        await pool.acquire("r1", tenant_id="t", workspace_id="w")
        session = mcp_fake.sessions[0]

        tracker = {"active": 0, "max": 0, "calls": 0}

        async def tracked_call(tool_name, arguments):
            tracker["active"] += 1
            tracker["max"] = max(tracker["max"], tracker["active"])
            tracker["calls"] += 1
            await asyncio.sleep(0.03)
            tracker["active"] -= 1
            return _result_ok(["done"])

        session.call_tool = AsyncMock(side_effect=tracked_call)
        lease = pool.get("r1")
        results = await asyncio.gather(*(lease.call_tool("browser_snapshot", {}) for _ in range(3)))

        assert tracker["calls"] == 3
        assert tracker["max"] == 1  # never overlapped inside one lease
        assert session.call_tool.await_count == 3
        assert len(results) == 3


class TestCommandBuild:
    def test_isolated_command_flags(self, tmp_path, monkeypatch):
        import harness.tools.browser_pool as bp

        monkeypatch.setattr(bp, "resolve_playwright_mcp_command", lambda explicit=None: ["fake-pw-mcp"])
        out = tmp_path / "out" / "t" / "r"
        pool = BrowserPool(make_config(tmp_path, headless=False))
        assert pool._build_command(None, out) == ["fake-pw-mcp", "--output-dir", str(out), "--isolated"]

    def test_isolated_headless_command(self, tmp_path, monkeypatch):
        import harness.tools.browser_pool as bp

        monkeypatch.setattr(bp, "resolve_playwright_mcp_command", lambda explicit=None: ["fake-pw-mcp"])
        out = tmp_path / "out"
        pool = BrowserPool(make_config(tmp_path, headless=True))
        assert pool._build_command(None, out) == ["fake-pw-mcp", "--headless", "--output-dir", str(out), "--isolated"]

    def test_persistent_command_uses_profile_dir(self, tmp_path, monkeypatch):
        import harness.tools.browser_pool as bp

        monkeypatch.setattr(bp, "resolve_playwright_mcp_command", lambda explicit=None: ["fake-pw-mcp"])
        out = tmp_path / "out"
        profile = tmp_path / "profiles" / "t" / "w"
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT))
        cmd = pool._build_command(profile, out)
        assert cmd[0] == "fake-pw-mcp"
        assert "--headless" in cmd  # make_config defaults headless=True
        assert cmd[cmd.index("--output-dir") + 1] == str(out)
        assert "--browser" in cmd and "chrome" in cmd
        assert cmd[cmd.index("--user-data-dir") + 1] == str(profile)


class TestPersistentProfileIsolation:
    async def test_profile_path_isolates_tenant_and_workspace(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT))
        l1 = await pool.acquire("r1", tenant_id="acme", workspace_id="proj-a")
        l2 = await pool.acquire("r2", tenant_id="acme", workspace_id="proj-b")
        l3 = await pool.acquire("r3", tenant_id="other", workspace_id="proj-a")

        root = (tmp_path / "profiles").resolve()
        assert l1._profile_dir == root / "acme" / "proj-a"
        assert l2._profile_dir == root / "acme" / "proj-b"
        assert l3._profile_dir == root / "other" / "proj-a"

    async def test_lock_file_records_owner_metadata(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT))
        await pool.acquire("r1", tenant_id="t", workspace_id="w")
        lock_file = _lock_file_for(tmp_path, "t", "w")
        assert lock_file.exists()
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
        assert payload["run_id"] == "r1"
        assert isinstance(payload["pid"], int)
        assert payload["created_at"] > 0

    async def test_release_removes_profile_lock(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT))
        await pool.acquire("r1", tenant_id="t", workspace_id="w")
        lock_file = _lock_file_for(tmp_path, "t", "w")
        assert lock_file.exists()
        await pool.release("r1")
        assert not lock_file.exists()

    async def test_second_run_same_profile_rejected_on_timeout(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT, lease_wait_ms=300))
        await pool.acquire("r1", tenant_id="t", workspace_id="w")

        with pytest.raises(BrowserLeaseUnavailableError) as exc:
            await pool.acquire("r2", tenant_id="t", workspace_id="w")
        message = str(exc.value)
        assert "in use by another run" in message
        assert "Use isolated mode" in message

    async def test_waiting_acquire_succeeds_when_lock_released(self, tmp_path, mcp_fake):
        # Simulate the owning process (another serve instance) going away: the
        # lock file disappears while run-2 waits → run-2 acquires and re-writes it.
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT, lease_wait_ms=8000))
        await pool.acquire("r1", tenant_id="t", workspace_id="w")
        lock_file = _lock_file_for(tmp_path, "t", "w")

        async def free_lock():
            await asyncio.sleep(0.15)
            lock_file.unlink(missing_ok=True)

        releaser = asyncio.create_task(free_lock())
        try:
            lease2 = await asyncio.wait_for(pool.acquire("r2", tenant_id="t", workspace_id="w"), timeout=5)
        finally:
            await releaser
        assert lease2.run_id == "r2"
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
        assert payload["run_id"] == "r2"

        await pool.release("r2")
        await pool.release("r1")

    async def test_isolated_mode_never_creates_profile_or_lock(self, tmp_path, mcp_fake):
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.ISOLATED))
        lease = await pool.acquire("r1", tenant_id="t", workspace_id="w")
        assert lease._profile_dir is None
        assert lease._lock_file is None
        assert not (tmp_path / "profiles").exists()

    def test_stale_profile_locks_reaped(self, tmp_path, monkeypatch):
        root = tmp_path / "profiles"
        root.mkdir(parents=True)
        fresh = root / "fresh.lock"
        stale = root / "stale.lock"
        fresh.write_text("x", encoding="utf-8")
        stale.write_text("x", encoding="utf-8")
        old = time.time() - 25 * 3600
        os.utime(stale, (old, old))

        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.PERSISTENT))
        asyncio.run(pool.reap_stale_profile_locks())
        assert fresh.exists()
        assert not stale.exists()


class TestCdpMode:
    async def test_cdp_returns_structured_not_implemented(self, tmp_path):
        pool = BrowserPool(make_config(tmp_path, mode=BrowserMode.CDP))
        with pytest.raises(BrowserLeaseUnavailableError) as exc:
            await pool.acquire("r1", tenant_id="t", workspace_id="w")
        assert "not implemented" in str(exc.value)
        assert "cdp" in str(exc.value).lower()


class TestTerminalEventRelease:
    @pytest.mark.parametrize(
        ("event_type", "payload"),
        [
            (EventType.RUN_COMPLETED, RunCompletedPayload(result_summary="done").model_dump()),
            (EventType.RUN_FAILED, RunFailedPayload(final_error="boom", event_count=0).model_dump()),
            (EventType.RUN_ORPHANED, RunOrphanedPayload().model_dump()),
        ],
        ids=["completed", "failed", "orphaned"],
    )
    async def test_terminal_event_triggers_release(self, tmp_path, mcp_fake, store, event_type, payload):
        pool = BrowserPool(make_config(tmp_path))
        pool.attach_store(store)
        await pool.acquire("run-evt", tenant_id="t", workspace_id="w")
        assert pool.get("run-evt") is not None

        await store.append_event("run-evt", event_type, payload)
        assert pool.get("run-evt") is None
        assert mcp_fake.session_exits == 1

    async def test_non_terminal_event_keeps_lease(self, tmp_path, mcp_fake, store):
        from harness.models.events import ToolCalledPayload

        pool = BrowserPool(make_config(tmp_path))
        pool.attach_store(store)
        await pool.acquire("run-mid", tenant_id="t", workspace_id="w")
        await store.append_event(
            "run-mid",
            EventType.TOOL_CALLED,
            ToolCalledPayload(tool_call_id="tc", tool_name="browser_navigate", input={}).model_dump(),
        )
        assert pool.get("run-mid") is not None
