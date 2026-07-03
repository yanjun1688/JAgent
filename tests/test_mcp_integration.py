"""Tests for MCP abstraction — MCPConfig, MCPServerManager, mcp_call tool."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.models.mcp_config import MCPConfig, MCPConnectionConfig
from harness.models.tools import SideEffect, ToolDefinition
from harness.tools.mcp_call import (
    MCP_CALL_DEF,
    connect_mcp_server,
    disconnect_mcp_server,
    get_manager,
    mcp_call_fn,
    set_manager,
)
from harness.tools.mcp_manager import MCPServerManager
from harness.tools.registry import ToolRegistry


# ── Helpers ───────────────────────────────────────────────────────────


def _make_mock_tool(name: str, desc: str = "", input_schema: dict | None = None) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = desc
    tool.inputSchema = input_schema or {}
    return tool


def _make_mock_result(*, texts: list[str] | None = None) -> MagicMock:
    result = MagicMock()
    result.isError = False
    content_items = []
    if texts:
        for t in texts:
            item = MagicMock()
            item.text = t
            item.type = "text"
            item.data = None
            content_items.append(item)
    result.content = content_items
    return result


def _make_mock_session(tools: list[MagicMock] | None = None) -> AsyncMock:
    session = AsyncMock()
    session.list_tools = AsyncMock(return_value=MagicMock(tools=tools or []))
    session.call_tool = AsyncMock(return_value=_make_mock_result(texts=["ok"]))
    return session


@pytest.fixture
def mock_stdio_patch():
    """Patch MCPServerManager._connect_stdio to return a _MCPSession wrapper."""
    from harness.tools.mcp_manager import _MCPSession
    session = _make_mock_session(tools=[_make_mock_tool("browser_navigate", "Navigate")])
    mcp_sess = _MCPSession(
        name="test",
        session=session,
        _transport_cm=AsyncMock(),
        _session_cm=AsyncMock(),
    )
    with patch.object(MCPServerManager, "_connect_stdio", AsyncMock(return_value=mcp_sess)):
        yield session


@pytest.fixture
def clean_manager():
    """Ensure no stale manager state between tests."""
    set_manager(None)
    yield
    set_manager(None)


# ═════════════════════════════════════════════════════════════════════
#  MCPConfig (model)
# ═════════════════════════════════════════════════════════════════════


class TestMCPConfig:
    def test_from_dict(self):
        data = {
            "servers": [
                {"name": "s1", "command": ["cmd1"], "enabled": True},
                {"name": "s2", "command": ["cmd2"], "url": "http://example.com/mcp"},
            ]
        }
        cfg = MCPConfig.from_dict(data)
        assert len(cfg.servers) == 2
        assert cfg.servers[0].name == "s1"
        assert cfg.servers[0].command == ["cmd1"]
        assert cfg.servers[0].url is None
        assert cfg.servers[1].name == "s2"
        assert cfg.servers[1].command == ["cmd2"]
        assert cfg.servers[1].url == "http://example.com/mcp"

    def test_from_file_valid(self):
        data = {"servers": [{"name": "p", "command": ["npx", "@playwright/mcp"]}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            tmp = f.name
        try:
            cfg = MCPConfig.from_file(tmp)
            assert len(cfg.servers) == 1
            assert cfg.servers[0].name == "p"
        finally:
            os.unlink(tmp)

    def test_from_file_not_found_returns_empty(self):
        cfg = MCPConfig.from_file("/nonexistent/path.json")
        assert len(cfg.servers) == 0

    def test_from_env_default_path(self):
        cfg = MCPConfig.from_env()
        assert isinstance(cfg, MCPConfig)

    def test_from_env_custom_path(self):
        data = {"servers": [{"name": "env-srv", "command": ["env-cmd"]}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            tmp = f.name
        try:
            with patch.dict(os.environ, {"HARNESS_MCP_CONFIG": tmp}):
                cfg = MCPConfig.from_env()
                assert len(cfg.servers) == 1
                assert cfg.servers[0].name == "env-srv"
        finally:
            os.unlink(tmp)

    def test_empty_servers_default(self):
        cfg = MCPConfig()
        assert cfg.servers == []

    def test_default_values(self):
        cfg = MCPConnectionConfig(name="test", command=["cmd"])
        assert cfg.enabled is True
        assert cfg.auto_register_tools is False
        assert cfg.timeout_ms == 120000
        assert cfg.environment == {}

    def test_connection_config_with_url_only(self):
        cfg = MCPConnectionConfig(name="sse-test", url="http://localhost:8080/mcp")
        assert cfg.command is None
        assert cfg.url == "http://localhost:8080/mcp"


# ═════════════════════════════════════════════════════════════════════
#  MCPServerManager
# ═════════════════════════════════════════════════════════════════════


class TestMCPServerManager:
    def test_init_empty(self):
        mgr = MCPServerManager(MCPConfig())
        assert mgr.server_names == []
        assert mgr.get_session("none") is None

    def test_init_with_registry(self):
        registry = ToolRegistry()
        mgr = MCPServerManager(MCPConfig(), registry=registry)
        assert mgr.registry is registry

    async def test_connect_server_success(self, mock_stdio_patch):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="test-srv", command=["test-cmd"], enabled=True),
        ])
        mgr = MCPServerManager(config)
        result = await mgr.connect_server(config.servers[0])

        assert result["success"] is True
        assert result["name"] == "test-srv"
        assert "tools" in result
        assert mgr.server_names == ["test-srv"]
        assert mgr.get_session("test-srv") is mock_stdio_patch

    async def test_connect_server_no_command_or_url(self):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="bad", enabled=True),
        ])
        mgr = MCPServerManager(config)
        result = await mgr.connect_server(config.servers[0])

        assert result["success"] is False
        assert "Either command or url" in result["error"]

    async def test_connect_server_failure(self):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="fail-srv", command=["crash"], enabled=True),
        ])
        mgr = MCPServerManager(config)
        with patch.object(mgr, "_connect_stdio", AsyncMock(side_effect=RuntimeError("connection failed"))):
            result = await mgr.connect_server(config.servers[0])
            assert result["success"] is False
            assert "connection failed" in result["error"]

    async def test_disconnect_server(self, mock_stdio_patch):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="d", command=["x"], enabled=True),
        ])
        mgr = MCPServerManager(config)
        await mgr.connect_server(config.servers[0])
        assert "d" in mgr.server_names

        result = await mgr.disconnect_server("d")
        assert result["success"] is True
        assert "d" not in mgr.server_names

    async def test_disconnect_nonexistent(self):
        mgr = MCPServerManager(MCPConfig())
        result = await mgr.disconnect_server("ghost")
        assert result["success"] is True

    async def test_start_all(self, mock_stdio_patch):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="s1", command=["c1"], enabled=True),
            MCPConnectionConfig(name="s2", command=["c2"], enabled=True),
        ])
        mgr = MCPServerManager(config)

        with patch.object(mgr, "connect_server", AsyncMock(side_effect=[
            {"name": "s1", "success": True, "tools": []},
            {"name": "s2", "success": True, "tools": []},
        ])):
            results = await mgr.start_all()
            assert len(results) == 2
            assert results[0]["success"] is True
            assert results[1]["success"] is True

    async def test_start_all_skips_disabled(self, mock_stdio_patch):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="s1", command=["c1"], enabled=True),
            MCPConnectionConfig(name="s2", command=["c2"], enabled=False),
        ])
        mgr = MCPServerManager(config)

        connect_calls = []

        async def tracking_connect(cfg):
            connect_calls.append(cfg.name)
            return {"name": cfg.name, "success": True, "tools": []}

        with patch.object(mgr, "connect_server", AsyncMock(side_effect=tracking_connect)):
            results = await mgr.start_all()
            assert len(results) == 1
            assert connect_calls == ["s1"]

    async def test_shutdown_all(self, mock_stdio_patch):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="a", command=["x"], enabled=True),
            MCPConnectionConfig(name="b", command=["y"], enabled=True),
        ])
        mgr = MCPServerManager(config)
        disconnect_calls = []

        async def tracking_disconnect(name):
            disconnect_calls.append(name)
            return {"name": name, "success": True}

        with patch.object(mgr, "disconnect_server", AsyncMock(side_effect=tracking_disconnect)):
            await mgr.start_all()
            assert set(mgr.server_names) == {"a", "b"}
            await mgr.shutdown_all()
            assert set(disconnect_calls) == {"a", "b"}

    async def test_auto_register_tools(self):
        registry = ToolRegistry()
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="auto-srv", command=["x"], enabled=True, auto_register_tools=True),
        ])
        mgr = MCPServerManager(config, registry=registry)
        tool_a = _make_mock_tool("tool_a", "Tool A", {"type": "object"})
        tool_b = _make_mock_tool("tool_b", "Tool B")

        session = _make_mock_session(tools=[tool_a, tool_b])
        with patch.object(mgr, "_connect_stdio", AsyncMock(return_value=session)):
            result = await mgr.connect_server(config.servers[0])
            assert result["success"] is True

        assert registry.get_tool_def("tool_a") is not None
        assert registry.get_tool_def("tool_b") is not None
        assert registry.get_tool_def("tool_a").description == "[auto-srv] Tool A"

    async def test_auto_register_skips_duplicates(self):
        registry = ToolRegistry()
        existing = ToolDefinition(
            name="existing_tool", description="", side_effects=[SideEffect.EXTERNAL],
        )
        registry.register(existing, lambda i: {})

        config = MCPConfig(servers=[
            MCPConnectionConfig(name="dup-srv", command=["x"], enabled=True, auto_register_tools=True),
        ])
        mgr = MCPServerManager(config, registry=registry)
        tool = _make_mock_tool("existing_tool")

        session = _make_mock_session(tools=[tool])
        with patch.object(mgr, "_connect_stdio", AsyncMock(return_value=session)):
            result = await mgr.connect_server(config.servers[0])
            assert result["success"] is True

        assert registry.get_tool_def("existing_tool") is existing

    async def test_get_session(self, mock_stdio_patch):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="s1", command=["x"], enabled=True),
        ])
        mgr = MCPServerManager(config)
        await mgr.connect_server(config.servers[0])
        assert mgr.get_session("s1") is mock_stdio_patch
        assert mgr.get_session("nonexistent") is None


# ═════════════════════════════════════════════════════════════════════
#  mcp_call_fn
# ═════════════════════════════════════════════════════════════════════


class TestMcpCallFn:
    async def test_no_manager(self, clean_manager):
        result = await mcp_call_fn({"tool_name": "test", "arguments": {}})
        assert result["success"] is False
        assert "not initialized" in result["error"]

    async def test_no_sessions(self, clean_manager):
        mgr = MCPServerManager(MCPConfig())
        set_manager(mgr)

        result = await mcp_call_fn({"tool_name": "test", "arguments": {}})
        assert result["success"] is False
        assert "No active MCP sessions" in result["error"]

    async def test_specific_server(self, clean_manager):
        session = _make_mock_session()
        session.call_tool = AsyncMock(return_value=_make_mock_result(texts=["hello world"]))
        mgr = MCPServerManager(MCPConfig())
        mgr._sessions["playwright"] = session
        set_manager(mgr)

        result = await mcp_call_fn({
            "server_name": "playwright",
            "tool_name": "browser_navigate",
            "arguments": {"url": "https://example.com"},
        })
        assert result["success"] is True
        assert result["content"] == ["hello world"]
        session.call_tool.assert_awaited_once_with("browser_navigate", {"url": "https://example.com"})

    async def test_specific_server_not_found(self, clean_manager):
        mgr = MCPServerManager(MCPConfig())
        mgr._sessions["other"] = _make_mock_session()
        set_manager(mgr)

        result = await mcp_call_fn({
            "server_name": "playwright",
            "tool_name": "browser_navigate",
        })
        assert result["success"] is False
        assert "No active MCP session for server 'playwright'" in result["error"]

    async def test_fallback_to_first_server(self, clean_manager):
        session = _make_mock_session()
        session.call_tool = AsyncMock(return_value=_make_mock_result(texts=["fallback result"]))
        mgr = MCPServerManager(MCPConfig())
        mgr._sessions["first-srv"] = session
        mgr._sessions["second-srv"] = _make_mock_session()
        set_manager(mgr)

        result = await mcp_call_fn({
            "tool_name": "some_tool",
        })
        assert result["success"] is True
        assert result["content"] == ["fallback result"]
        session.call_tool.assert_awaited_once()

    async def test_content_with_data_items(self, clean_manager):
        session = _make_mock_session()

        result_mock = MagicMock()
        data_item = MagicMock()
        data_item.text = None
        data_item.data = b"binary data"
        data_item.type = "resource"
        result_mock.content = [data_item]

        session.call_tool = AsyncMock(return_value=result_mock)
        mgr = MCPServerManager(MCPConfig())
        mgr._sessions["srv"] = session
        set_manager(mgr)

        result = await mcp_call_fn({"tool_name": "get_resource"})
        assert result["success"] is True
        assert result["content"] == ["b'binary data'"]

    async def test_content_with_mixed_items(self, clean_manager):
        session = _make_mock_session()

        result_mock = MagicMock()
        item1 = MagicMock()
        item1.text = "text part"
        item1.data = None
        item1.type = "text"
        item2 = MagicMock()
        item2.text = None
        item2.data = "raw data"
        item2.type = "other"
        item3 = MagicMock()
        item3.text = None
        item3.data = None
        item3.type = "unknown"
        result_mock.content = [item1, item2, item3]

        session.call_tool = AsyncMock(return_value=result_mock)
        mgr = MCPServerManager(MCPConfig())
        mgr._sessions["srv"] = session
        set_manager(mgr)

        result = await mcp_call_fn({"tool_name": "mixed"})
        assert result["success"] is True
        assert len(result["content"]) == 3
        assert result["content"][0] == "text part"

    async def test_tool_call_exception(self, clean_manager):
        session = _make_mock_session()
        session.call_tool = AsyncMock(side_effect=RuntimeError("MCP error"))
        mgr = MCPServerManager(MCPConfig())
        mgr._sessions["srv"] = session
        set_manager(mgr)

        result = await mcp_call_fn({"tool_name": "crash"})
        assert result["success"] is False
        assert "MCP tool 'crash' failed" in result["error"]
        assert "MCP error" in result["error"]


# ═════════════════════════════════════════════════════════════════════
#  connect_mcp_server / disconnect_mcp_server (wrapper functions)
# ═════════════════════════════════════════════════════════════════════


class TestConnectDisconnectWrappers:
    async def test_connect_wrapper_creates_manager(self, clean_manager):
        assert get_manager() is None

        with patch.object(MCPServerManager, "connect_server", AsyncMock(return_value={
            "name": "test", "success": True, "tools": [],
        })):
            result = await connect_mcp_server("test", command=["test-cmd"])
            assert result["success"] is True
            assert get_manager() is not None

    async def test_connect_wrapper_reuses_manager(self, clean_manager):
        existing = MCPServerManager(MCPConfig())
        set_manager(existing)

        with patch.object(existing, "connect_server", AsyncMock(return_value={
            "name": "new-srv", "success": True, "tools": [],
        })):
            result = await connect_mcp_server("new-srv", command=["cmd"])
            assert result["success"] is True
            assert get_manager() is existing

    async def test_disconnect_wrapper_no_manager(self, clean_manager):
        result = await disconnect_mcp_server("test")
        assert result["success"] is False
        assert "not initialized" in result["error"]

    async def test_disconnect_wrapper_delegates(self, clean_manager):
        mgr = MCPServerManager(MCPConfig())
        set_manager(mgr)

        with patch.object(mgr, "disconnect_server", AsyncMock(return_value={
            "name": "srv", "success": True,
        })):
            result = await disconnect_mcp_server("srv")
            assert result["success"] is True


# ═════════════════════════════════════════════════════════════════════
#  MCP_CALL_DEF schema
# ═════════════════════════════════════════════════════════════════════


class TestMCPCallDefinition:
    def test_definition_fields(self):
        assert MCP_CALL_DEF.name == "mcp_call"
        assert MCP_CALL_DEF.idempotency_key_fields == ["server_name", "tool_name", "arguments"]
        assert SideEffect.EXTERNAL in MCP_CALL_DEF.side_effects
        assert MCP_CALL_DEF.timeout_ms == 60000

    def test_schema_requires_tool_name(self):
        assert MCP_CALL_DEF.input_schema["required"] == ["tool_name"]

    def test_schema_properties(self):
        props = MCP_CALL_DEF.input_schema["properties"]
        assert "server_name" in props
        assert "tool_name" in props
        assert "arguments" in props

    def test_output_schema(self):
        output = MCP_CALL_DEF.output_schema["properties"]
        assert "success" in output
        assert "content" in output
        assert "error" in output

    def test_guardrails_configured(self):
        assert MCP_CALL_DEF.guardrails is not None
        assert len(MCP_CALL_DEF.guardrails) >= 1
        assert MCP_CALL_DEF.guardrails[0].guardrail_type == "scope"

    def test_idempotency_includes_arguments(self):
        """Idempotency key must cover the full input to prevent duplicate MCP calls."""
        fields = MCP_CALL_DEF.idempotency_key_fields
        assert "arguments" in fields
        assert "tool_name" in fields


# ═════════════════════════════════════════════════════════════════════
#  Integration: MCPServerManager + mcp_call_fn
# ═════════════════════════════════════════════════════════════════════


class TestManagerWithCallFn:
    async def test_full_flow(self, mock_stdio_patch, clean_manager):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="playwright", command=["npx", "@playwright/mcp"], enabled=True),
        ])
        mgr = MCPServerManager(config)
        result = await mgr.connect_server(config.servers[0])
        assert result["success"] is True
        set_manager(mgr)

        call_result = await mcp_call_fn({
            "server_name": "playwright",
            "tool_name": "browser_navigate",
            "arguments": {"url": "https://example.com"},
        })
        assert call_result["success"] is True

    async def test_start_all_and_call(self, mock_stdio_patch, clean_manager):
        config = MCPConfig(servers=[
            MCPConnectionConfig(name="srv1", command=["x"], enabled=True),
        ])
        mgr = MCPServerManager(config)
        set_manager(mgr)

        with patch.object(mgr, "connect_server", AsyncMock(return_value={
            "name": "srv1", "success": True, "tools": [{"name": "t1"}],
        })):
            results = await mgr.start_all()
            assert results[0]["success"] is True

        mgr._sessions["srv1"] = mock_stdio_patch
        result = await mcp_call_fn({"tool_name": "browser_navigate", "arguments": {"url": "http://test"}})
        assert result["success"] is True
