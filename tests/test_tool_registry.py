"""Feature: ToolRegistry 统一注册入口（ADR-010 D-03 / D-07）

行为分层（Given/When/Then）：
  1. register_tool(BaseTool) → 注册定义 + invoker
  2. invoker 调用 → dispatch 到工具 run()/@operation
  3. 重复注册同名 → ValueError
  4. list_tool_defs / list_tool_fns 返回已注册内容
  5. register_tool 生成的 ToolDefinition 与 to_definition() 一致
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect
from harness.tools.base import BaseTool, operation
from harness.tools.registry import ToolRegistry


class _PingTool(BaseTool):
    name = "ping_tool"
    description = "Ping"
    input_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "msg": {"type": "string"}},
        "required": ["action"],
    }
    output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    operation_key = "action"
    side_effects = [SideEffect.EXTERNAL]

    @operation("ping", probe_allowed=True)
    async def ping(self, input):
        return {"ok": True, "echo": input.get("msg")}


class TestRegisterTool:
    @pytest.mark.asyncio
    async def test_given_base_tool_when_register_tool_then_def_and_invoker_registered(self):
        # Given 一个 BaseTool 实例
        registry = ToolRegistry()
        # When 注册
        name = registry.register_tool(_PingTool())
        # Then 定义与 invoker 均已注册
        assert name == "ping_tool"
        assert registry.get_tool_def("ping_tool") is not None
        assert registry.get_tool_fn("ping_tool") is not None

    @pytest.mark.asyncio
    async def test_given_registered_tool_when_invoker_called_then_dispatches(self):
        # Given 已注册工具
        registry = ToolRegistry()
        registry.register_tool(_PingTool())
        invoker = registry.get_tool_fn("ping_tool")
        # When 调用 invoker
        result = await invoker({"action": "ping", "msg": "hi"})
        # Then dispatch 到 ping 方法
        assert result == {"ok": True, "echo": "hi"}

    @pytest.mark.asyncio
    async def test_given_duplicate_name_when_register_tool_then_raises(self):
        # Given 已注册 ping_tool
        registry = ToolRegistry()
        registry.register_tool(_PingTool())
        # When 再次注册同名
        with pytest.raises(ValueError):
            registry.register_tool(_PingTool())

    @pytest.mark.asyncio
    async def test_given_registered_tool_when_list_then_returns_both(self):
        # Given 已注册工具
        registry = ToolRegistry()
        registry.register_tool(_PingTool())
        # When 列出
        defs = registry.list_tool_defs()
        fns = registry.list_tool_fns()
        # Then 返回定义与函数
        assert [d.name for d in defs] == ["ping_tool"]
        assert "ping_tool" in fns

    def test_given_tool_when_register_tool_then_definition_matches_to_definition(self):
        # Given 工具实例
        tool = _PingTool()
        registry = ToolRegistry()
        # When 注册
        registry.register_tool(tool)
        # Then 存储的 ToolDefinition 与 to_definition() 一致
        td = registry.get_tool_def("ping_tool")
        expected = tool.to_definition()
        assert td.name == expected.name
        assert td.operation_key == expected.operation_key
        assert {o.operation for o in td.operations} == {o.operation for o in expected.operations}
