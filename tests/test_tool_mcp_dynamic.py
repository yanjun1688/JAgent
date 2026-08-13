"""Feature: MCP 动态工具经 register_tool 注册（ADR-010 D-05）

行为分层（Given/When/Then）：
  1. McpDynamicTool → to_definition 合成契约（name/description/input_schema/needs_mcp_manager）
  2. register_tool(McpDynamicTool) → invoker 非 None（杜绝旧 fn=None 无法执行）
  3. mcp_manager 注入 → invoke 经 mcp_call 逻辑执行
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect
from harness.tools.mcp_call import McpDynamicTool
from harness.tools.registry import ToolRegistry


class TestMcpDynamicTool:
    def test_given_mcp_dynamic_tool_when_to_definition_then_contract_synthesized(self):
        # Given 动态工具声明（来自 MCP 服务器）
        tool = McpDynamicTool(
            name="get_weather",
            description="[weather] Get weather",
            input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
            server_name="weather",
        )
        # When 合成
        td = tool.to_definition()
        # Then 契约正确
        assert td.name == "get_weather"
        assert td.description == "[weather] Get weather"
        assert td.side_effects == [SideEffect.EXTERNAL]
        assert McpDynamicTool.needs_mcp_manager is True

    def test_given_mcp_dynamic_tool_when_register_tool_then_invoker_non_none(self):
        # Given 动态工具
        registry = ToolRegistry()
        # When 经 register_tool 注册
        registry.register_tool(
            McpDynamicTool(name="get_weather", description="d", input_schema={}, server_name="weather")
        )
        # Then invoker 非 None（杜绝旧 fn=None 无法执行）
        assert registry.get_tool_fn("get_weather") is not None

    @pytest.mark.asyncio
    async def test_given_manager_injected_when_invoke_then_delegates_to_mcp_call(self, monkeypatch):
        # Given 动态工具
        tool = McpDynamicTool(name="get_weather", description="d", input_schema={}, server_name="weather")
        calls = {}

        async def fake_mcp_call(input, *, manager=None):
            calls["input"] = input
            calls["manager"] = manager
            return {"success": True, "content": ["sunny"]}

        monkeypatch.setattr("harness.tools.mcp_call.mcp_call_fn", fake_mcp_call)
        # When invoke（mcp_manager 注入）
        manager = object()
        result = await tool.invoke({"city": "beijing"}, mcp_manager=manager)
        # Then 委托给 mcp_call 逻辑，携带 server_name/tool_name/arguments
        assert result["content"] == ["sunny"]
        assert calls["input"] == {"server_name": "weather", "tool_name": "get_weather", "arguments": {"city": "beijing"}}
        assert calls["manager"] is manager
