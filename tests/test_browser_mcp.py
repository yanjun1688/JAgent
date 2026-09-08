"""ADR-011 §3.3 integration tests — BrowserMcpTool routing + register_browser_tools
policy filtering (first-class BaseTool registration).

``BrowserPool`` instances are real but their spawn/discovery boundary is mocked,
so no Chrome or node subprocess runs. Cross-platform green on Linux CI.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from harness.models.browser import BrowserConfig, BrowserMode
from harness.models.tools import SideEffect
from harness.tools.browser_command import BrowserCommandNotFoundError
from harness.tools.browser_mcp import BrowserMcpTool, register_browser_tools
from harness.tools.browser_policy import policy_for
from harness.tools.browser_pool import BrowserLease, BrowserLeaseUnavailableError, BrowserPool, set_pool
from harness.tools.executor import current_run_id
from harness.tools.registry import ToolRegistry

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

EXPECTED_BLOCKED = {"browser_run_code_unsafe", "browser_evaluate"}


def _tool(name: str, desc: str = "desc") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=desc, inputSchema={"type": "object"})


def _discovered_tools() -> list[SimpleNamespace]:
    return [_tool(n, f"playwright {n}") for n in BROWSER_TOOL_NAMES]


def _result(texts: list[str] | None = None, *, is_error: bool = False) -> MagicMock:
    result = MagicMock()
    result.isError = is_error
    items = []
    for t in texts or []:
        item = MagicMock()
        item.text = t
        item.data = None
        items.append(item)
    result.content = items
    return result


def _make_pool(*, allow_evaluate: bool = False, mode: BrowserMode = BrowserMode.ISOLATED) -> BrowserPool:
    return BrowserPool(BrowserConfig(mode=mode, allow_evaluate=allow_evaluate))


def _patch_spawn(pool: BrowserPool, session: AsyncMock, tool_names: list[str]) -> None:
    async def fake_spawn(run_id, tenant_id, workspace_id, profile_dir, lock_file):
        return BrowserLease(
            run_id=run_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            mode=pool.config.mode,
            session=session,
            tool_names=tool_names,
            _transport_cm=AsyncMock(),
            _session_cm=AsyncMock(),
        )

    pool._spawn_lease = fake_spawn  # type: ignore[method-assign]


def _run_sync(tool: BrowserMcpTool, input: dict) -> dict:
    return asyncio.run(tool.run(input))


class TestBrowserMcpToolRouting:
    """Run-time routing: no-lease / no-pool / cross-server guard → structured errors."""

    def test_no_active_run_or_pool_returns_structured_error(self):
        set_pool(None)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        result = _run_sync(tool, {})
        assert result["success"] is False
        assert "not available" in result["error"]

    def test_no_run_id_returns_structured_error(self):
        pool = _make_pool()
        set_pool(pool)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        try:
            result = _run_sync(tool, {})
        finally:
            set_pool(None)
        assert result["success"] is False
        assert "not available" in result["error"]

    def test_cdp_lease_unavailable_is_structured(self):
        pool = _make_pool(mode=BrowserMode.CDP)
        set_pool(pool)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        try:
            token = current_run_id.set("run-cdp")
            result = _run_sync(tool, {})
        finally:
            current_run_id.reset(token)
            set_pool(None)
        assert result["success"] is False
        assert "not implemented" in result["error"]

    def test_acquire_raises_surfaces_lease_error(self):
        pool = _make_pool()

        async def boom(*args, **kwargs):
            raise BrowserLeaseUnavailableError("profile busy")

        pool.acquire = boom  # type: ignore[method-assign]
        set_pool(pool)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        try:
            token = current_run_id.set("run-x")
            result = _run_sync(tool, {})
        finally:
            current_run_id.reset(token)
            set_pool(None)
        assert result["success"] is False
        assert "profile busy" in result["error"]

    def test_tool_not_exposed_by_connected_server(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=_result(["x"]))
        pool = _make_pool()
        _patch_spawn(pool, session, tool_names=["browser_click"])
        set_pool(pool)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        try:
            token = current_run_id.set("run-y")
            result = _run_sync(tool, {"url": "u"})
        finally:
            current_run_id.reset(token)
            set_pool(None)
        assert result["success"] is False
        assert "not exposed" in result["error"]
        session.call_tool.assert_not_awaited()

    def test_success_returns_content_and_acquires_lease(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=_result(["hello world"]))
        pool = _make_pool()
        _patch_spawn(pool, session, tool_names=["browser_navigate"])
        set_pool(pool)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        try:
            token = current_run_id.set("run-ok")
            result = _run_sync(tool, {"url": "https://example.com"})
        finally:
            current_run_id.reset(token)
            set_pool(None)
        assert result["success"] is True
        assert result["content"] == ["hello world"]
        session.call_tool.assert_awaited_once_with("browser_navigate", {"url": "https://example.com"})

    def test_mcp_error_result_maps_to_structured_failure(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=_result(["bad page"], is_error=True))
        pool = _make_pool()
        _patch_spawn(pool, session, tool_names=["browser_navigate"])
        set_pool(pool)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        try:
            token = current_run_id.set("run-err")
            result = _run_sync(tool, {})
        finally:
            current_run_id.reset(token)
            set_pool(None)
        assert result["success"] is False
        assert "bad page" in result["error"]

    def test_call_exception_is_wrapped_not_leaked(self):
        session = AsyncMock()
        session.call_tool = AsyncMock(side_effect=RuntimeError("raw internal boom"))
        pool = _make_pool()
        _patch_spawn(pool, session, tool_names=["browser_navigate"])
        set_pool(pool)
        tool = BrowserMcpTool("browser_navigate", "d", {}, policy_for("browser_navigate"))
        try:
            token = current_run_id.set("run-exc")
            result = _run_sync(tool, {})
        finally:
            current_run_id.reset(token)
            set_pool(None)
        assert result["success"] is False
        assert "failed" in result["error"]
        assert "raw internal boom" in result["error"]


class TestRegisterBrowserTools:
    """Startup registration: discovered 24 tools → policy-filtered registry."""

    async def test_full_discovery_filters_blocked_and_sets_contracts(self):
        pool = _make_pool()
        pool.discover_tools = AsyncMock(return_value=_discovered_tools())  # type: ignore[method-assign]
        registry = ToolRegistry()

        report = await register_browser_tools(registry, pool)

        assert report["success"] is True
        assert len(report["registered"]) == 22
        blocked_names = {b["name"] for b in report["blocked"]}
        assert blocked_names == EXPECTED_BLOCKED

        names = set(registry.tool_names)
        assert "browser_run_code_unsafe" not in names
        assert "browser_evaluate" not in names
        assert "browser_navigate" in names

        # file_upload requires human confirmation.
        upload_def = registry.get_tool_def("browser_file_upload")
        assert upload_def.requires_confirmation is True
        assert upload_def.side_effects == [SideEffect.EXTERNAL]

        # read-only tools carry no side effects.
        assert registry.get_tool_def("browser_snapshot").side_effects == []

        # stateful tool contract: external + serialized + no idempotency cache.
        nav_def = registry.get_tool_def("browser_navigate")
        assert nav_def.side_effects == [SideEffect.EXTERNAL]
        assert nav_def.max_parallel == 1
        assert nav_def.idempotency_key_fields == []

        # every registered browser tool is serialized and never idempotency-cached.
        for name in report["registered"]:
            td = registry.get_tool_def(name)
            assert td.max_parallel == 1, name
            assert td.idempotency_key_fields == [], name

    async def test_allow_evaluate_registers_evaluate_still_blocks_unsafe(self):
        pool = _make_pool(allow_evaluate=True)
        pool.discover_tools = AsyncMock(return_value=_discovered_tools())  # type: ignore[method-assign]
        registry = ToolRegistry()

        report = await register_browser_tools(registry, pool)

        assert report["success"] is True
        blocked_names = {b["name"] for b in report["blocked"]}
        assert blocked_names == {"browser_run_code_unsafe"}
        assert "browser_run_code_unsafe" not in registry.tool_names
        assert registry.get_tool_def("browser_evaluate") is not None
        assert registry.get_tool_def("browser_evaluate").requires_confirmation is True

    async def test_pool_not_initialized_soft_failure(self):
        set_pool(None)
        registry = ToolRegistry()
        report = await register_browser_tools(registry)
        assert report["success"] is False
        assert "browser pool not initialized" in report["error"]
        assert report["registered"] == []

    async def test_non_browser_pool_rejected(self):
        registry = ToolRegistry()
        report = await register_browser_tools(registry, pool=MagicMock())
        assert report["success"] is False
        assert "browser pool not initialized" in report["error"]

    async def test_discovery_binary_missing_is_soft_failure(self):
        pool = _make_pool()
        pool.discover_tools = AsyncMock(  # type: ignore[method-assign]
            side_effect=BrowserCommandNotFoundError("run the install script")
        )
        registry = ToolRegistry()
        report = await register_browser_tools(registry, pool)
        assert report["success"] is False
        assert "run the install script" in report["error"]
        assert registry.tool_names == []

    async def test_discovery_generic_failure_is_soft(self):
        pool = _make_pool()
        pool.discover_tools = AsyncMock(side_effect=RuntimeError("spawn crashed"))  # type: ignore[method-assign]
        registry = ToolRegistry()
        report = await register_browser_tools(registry, pool)
        assert report["success"] is False
        assert "playwright-mcp discovery failed" in report["error"]

    async def test_duplicate_registration_is_skipped(self):
        pool = _make_pool()
        pool.discover_tools = AsyncMock(return_value=_discovered_tools())  # type: ignore[method-assign]
        registry = ToolRegistry()
        await register_browser_tools(registry, pool)
        report = await register_browser_tools(registry, pool)
        assert report["success"] is True
        assert len(report["registered"]) == 0
        assert len(registry.tool_names) == 22
