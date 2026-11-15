"""MCP 适配器回归测试（不联网）。

用 monkeypatch 替换 `httpx.post`，验证：
* `tools/list` 发现 → 注册进 ToolRegistry，名字带服务前缀；
* `tools/call` 成功 → `ToolResult.data["data"]` 是载荷；
* 业务码非 0 / isError / HTTP 4xx → 收敛成 ToolResult，不抛穿；
* `mcp_servers_from_env` 的开关与 key 识别（含 `.env` 里的 `tonghuashun-key`）。
"""

from __future__ import annotations

import httpx

from jagent.adapters.mcp import (
    McpServer,
    mcp_servers_from_env,
    register_mcp_tools,
)
from jagent.harness.registry import ToolRegistry

TOOL = {
    "name": "echo",
    "description": "echo back",
    "inputSchema": {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    },
}


def _ok(data):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 3, "result": data})


def _make_post(*, business_code=0, is_error=False, status=200):
    def fake_post(url, json=None, headers=None, timeout=None):
        payload = json or {}
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": [TOOL]}})
        if method == "tools/call":
            if status >= 400:
                return httpx.Response(status, text="denied")
            args = (payload.get("params") or {}).get("arguments") or {}
            result = {
                "isError": is_error,
                "structuredContent": {
                    "code": business_code,
                    "message": "success" if business_code == 0 else "boom",
                    "data": {"echo": args.get("q")} if business_code == 0 else None,
                },
            }
            return _ok(result)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})

    return fake_post


def _register(monkeypatch, **kwargs) -> tuple[ToolRegistry, list[str]]:
    monkeypatch.setattr("jagent.adapters.mcp.httpx.post", _make_post(**kwargs))
    registry = ToolRegistry()
    registered = register_mcp_tools(registry, [McpServer("fake", "http://fake", "k")])
    return registry, registered


def test_discovery_registers_prefixed_tool(monkeypatch):
    registry, registered = _register(monkeypatch)
    assert registered == ["fake.echo"]
    spec = registry.get("fake.echo")
    assert spec.readonly is True
    assert spec.side_effect.value == "none"
    assert spec.observations == ("ok", "data")
    assert spec.args_schema["required"] == ["q"]


def test_successful_call_returns_payload_under_data(monkeypatch):
    registry, _ = _register(monkeypatch)
    result = registry.call("fake.echo", {"q": "hi"}, workspace=".", timeout=5)
    assert result.ok is True
    assert result.data == {"data": {"echo": "hi"}}


def test_business_error_becomes_tool_result(monkeypatch):
    registry, _ = _register(monkeypatch, business_code=2003)
    result = registry.call("fake.echo", {"q": "hi"}, workspace=".", timeout=5)
    assert result.ok is False
    assert result.error_code == "tool_exception"
    assert "boom" in result.error_message


def test_is_error_becomes_tool_result(monkeypatch):
    registry, _ = _register(monkeypatch, is_error=True)
    result = registry.call("fake.echo", {"q": "hi"}, workspace=".", timeout=5)
    assert result.ok is False
    assert result.error_code == "tool_exception"


def test_http_403_maps_to_permission_denied(monkeypatch):
    # 发现阶段正常（initialize / tools/list 200），调用阶段 403 → permission_denied。
    registry, registered = _register(monkeypatch, status=403)
    assert registered == ["fake.echo"]
    result = registry.call("fake.echo", {"q": "hi"}, workspace=".", timeout=5)
    assert result.ok is False
    assert result.error_code == "permission_denied"


def test_servers_from_env_uses_configured_endpoints():
    env = {
        "JAGENT_MCP_API_KEY": "secret",
        "JAGENT_MCP_META_URL": "https://meta.example/mcp",
        "JAGENT_MCP_A_SHARE_URL": "https://a.example/mcp",
    }
    servers = mcp_servers_from_env(env)
    assert [s.name for s in servers] == ["meta", "a_share"]
    assert all(s.api_key == "secret" for s in servers)
    assert servers[0].url == "https://meta.example/mcp"
    assert servers[1].url == "https://a.example/mcp"


def test_servers_from_env_has_no_hardcoded_endpoint():
    """只给 key 不给端点：不接任何服务（部署地址只能来自配置）。"""
    assert mcp_servers_from_env({"JAGENT_MCP_API_KEY": "secret"}) == []


def test_servers_from_env_accepts_legacy_key_alias():
    env = {
        "tonghuashun-key": "secret",
        "JAGENT_MCP_META_URL": "https://meta.example/mcp",
    }
    servers = mcp_servers_from_env(env)
    assert [s.name for s in servers] == ["meta"]
    assert servers[0].api_key == "secret"


def test_servers_from_env_can_be_disabled():
    env = {"JAGENT_MCP_API_KEY": "x", "JAGENT_MCP_META_URL": "https://meta.example/mcp"}
    assert mcp_servers_from_env({**env, "JAGENT_MCP_ENABLED": "0"}) == []
    assert mcp_servers_from_env({}) == []
