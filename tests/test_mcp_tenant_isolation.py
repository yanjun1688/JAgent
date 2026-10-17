"""MCP memory tenant isolation (findings 9/17/33)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.core.tenant import reset_current_principal, set_current_principal
from harness.models.mcp_config import MCPConfig, MCPConnectionConfig
from harness.models.principal import Principal, Role
from harness.tools.mcp_call import mcp_call_fn
from harness.tools.mcp_manager import MCPServerManager


def _memory_cfg() -> MCPConnectionConfig:
    return MCPConnectionConfig(
        name="memory",
        command=["npx", "-y", "@modelcontextprotocol/server-memory"],
        enabled=True,
        tenant_scoped=True,
        environment={"MEMORY_FILE_PATH": "{tenant_state_dir}/memory.jsonl"},
    )


def _fake_session(label: str) -> MagicMock:
    session = MagicMock()
    session.list_tools = AsyncMock(
        return_value=SimpleNamespace(
            tools=[SimpleNamespace(name="read_graph", description="", inputSchema={})],
        )
    )
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=label)],
        )
    )
    return session


def _manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured: list[MCPConnectionConfig]) -> MCPServerManager:
    monkeypatch.setenv("JAGENT_MCP_STATE_DIR", str(tmp_path))

    async def fake_connect(cfg: MCPConnectionConfig):
        captured.append(cfg)
        return _fake_session(cfg.name)

    manager = MCPServerManager(MCPConfig(servers=[_memory_cfg()]))
    monkeypatch.setattr(manager, "_connect_stdio", fake_connect)
    return manager


async def test_start_all_discovers_tenant_scoped_tools_without_retaining_session(monkeypatch, tmp_path):
    captured: list[MCPConnectionConfig] = []
    manager = _manager(monkeypatch, tmp_path, captured)

    results = await manager.start_all()

    assert len(results) == 1
    assert results[0]["success"] is True
    assert manager.server_names == []
    assert [item.name for item in captured] == ["memory@__mcp_discovery__"]
    assert manager._sessions == {}


async def test_tenant_memory_paths_are_isolated(monkeypatch, tmp_path):
    captured: list[MCPConnectionConfig] = []
    manager = _manager(monkeypatch, tmp_path, captured)
    cfg = _memory_cfg()

    await manager.connect_server(cfg, tenant_id="tenant-a")
    await manager.connect_server(cfg, tenant_id="tenant-b")

    paths = [Path(item.environment["MEMORY_FILE_PATH"]) for item in captured]
    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert paths[0].parent.name == "tenant-a"
    assert paths[1].parent.name == "tenant-b"
    assert paths[0].name == "memory.jsonl"
    assert paths[0].parent.is_dir()
    assert manager.server_names == []
    assert set(manager._sessions) == {"memory@tenant-a", "memory@tenant-b"}


async def test_get_session_for_tenant_is_lazy_and_cached(monkeypatch, tmp_path):
    captured: list[MCPConnectionConfig] = []
    manager = _manager(monkeypatch, tmp_path, captured)

    first = await manager.get_session_for_tenant("memory", "t1")
    again = await manager.get_session_for_tenant("memory", "t1")
    other = await manager.get_session_for_tenant("memory", "t2")

    assert first is again
    assert first is not other
    assert len(captured) == 2
    assert await manager.get_session_for_tenant("memory", None) is None


async def test_tenant_scoped_connect_requires_tenant(monkeypatch, tmp_path):
    captured: list[MCPConnectionConfig] = []
    manager = _manager(monkeypatch, tmp_path, captured)

    result = await manager.connect_server(_memory_cfg())

    assert result["success"] is False
    assert "tenant context" in result["error"]
    assert captured == []


async def test_tenant_scoped_requires_state_dir_placeholder(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path, [])
    cfg = MCPConnectionConfig(name="memory", command=["x"], enabled=True, tenant_scoped=True)

    result = await manager.connect_server(cfg, tenant_id="t1")

    assert result["success"] is False
    assert "tenant_state_dir" in result["error"]


async def test_mcp_call_fails_closed_without_tenant(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path, [])

    result = await mcp_call_fn({"server_name": "memory", "tool_name": "read_graph"}, manager=manager)

    assert result["success"] is False
    assert "tenant context" in result["error"]


async def test_mcp_call_uses_tenant_scoped_session(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path, [])
    token = set_current_principal(
        Principal(subject="ops:test", tenant_id="tenant-a", roles={Role.ADMIN}, auth_method="test")
    )
    try:
        result = await mcp_call_fn({"server_name": "memory", "tool_name": "read_graph"}, manager=manager)
    finally:
        reset_current_principal(token)

    assert result["success"] is True
    assert result["content"] == ["memory@tenant-a"]
    assert "memory@tenant-a" in manager._sessions


async def test_disconnect_closes_all_tenant_sessions(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path, [])
    cfg = _memory_cfg()
    await manager.connect_server(cfg, tenant_id="tenant-a")
    await manager.connect_server(cfg, tenant_id="tenant-b")

    await manager.disconnect_server("memory")

    assert manager._sessions == {}


def test_available_server_names_orders_config_and_hides_scoped_names(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path, [])
    manager.config.servers.append(
        MCPConnectionConfig(name="fetch", command=["x"], enabled=True)
    )
    manager._sessions["fetch"] = _fake_session("fetch")

    assert manager.available_server_names("tenant-a") == ["memory", "fetch"]
    assert manager.available_server_names(None) == ["fetch"]
