"""Feature: BaseTool 声明式工具抽象（ADR-010 D-01）

行为分层（Given/When/Then）：
  1. 类属性声明契约 → to_definition() 合成 ToolDefinition
  2. @operation 方法声明 per-operation 契约，未声明 output_schema 时继承工具级
  3. run() 按 operation_key 分发到对应 @operation 方法
  4. 无 operation 工具覆写 run()，invoke() 调用之
  5. needs_backend 声明后 invoke 注入 backend
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect
from harness.tools.base import BaseTool, operation


class _EchoOpTool(BaseTool):
    """声明 operation_key + @operation 的示例工具。"""

    name = "echo_op"
    description = "Echo with operations"
    input_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "msg": {"type": "string"}},
        "required": ["action"],
    }
    output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    operation_key = "action"
    side_effects = [SideEffect.EXTERNAL]
    idempotency_key_fields = ["action", "msg"]
    needs_backend = True

    @operation("ping", probe_allowed=True)
    async def ping(self, input):
        return {"ok": True, "echo": input.get("msg")}

    @operation("pong", side_effects=[SideEffect.EXTERNAL])
    async def pong(self, input):
        return {"ok": True}


class _NoOpTool(BaseTool):
    """无 operation，直接覆写 run() 的示例工具。"""

    name = "noop"
    description = "No operation"
    input_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    output_schema = {"type": "object", "properties": {"result": {"type": "string"}}}

    async def run(self, input):
        return {"result": input.get("x")}


class TestToDefinition:
    """Feature: to_definition 合成契约"""

    def test_given_tool_declares_contract_when_to_definition_then_builds_definition(self):
        # Given 一个声明契约的 BaseTool 子类
        tool = _EchoOpTool()
        # When 合成 ToolDefinition
        td = tool.to_definition()
        # Then 契约字段正确映射
        assert td.name == "echo_op"
        assert td.description == "Echo with operations"
        assert td.input_schema["required"] == ["action"]
        assert td.output_schema["properties"]["ok"]["type"] == "boolean"
        assert td.operation_key == "action"
        assert td.side_effects == [SideEffect.EXTERNAL]
        assert td.idempotency_key_fields == ["action", "msg"]

    def test_given_operation_methods_when_to_definition_then_builds_operations(self):
        # Given @operation 标记的两个方法
        td = _EchoOpTool().to_definition()
        # When 合成
        ops = {o.operation: o for o in td.operations}
        # Then 生成对应 OperationContract
        assert len(td.operations) == 2
        assert ops["ping"].probe_allowed is True
        assert ops["ping"].side_effects == []
        assert ops["pong"].side_effects == [SideEffect.EXTERNAL]

    def test_given_operation_without_output_schema_when_to_definition_then_inherits_tool_schema(self):
        # Given @operation 未声明 output_schema
        td = _EchoOpTool().to_definition()
        # When 合成
        ops = {o.operation: o for o in td.operations}
        # Then 继承工具级 output_schema（消灭重复样板）
        assert ops["ping"].output_schema == _EchoOpTool.output_schema
        assert ops["pong"].output_schema == _EchoOpTool.output_schema


class TestDispatch:
    """Feature: operation 分发"""

    @pytest.mark.asyncio
    async def test_given_discriminant_input_when_run_then_dispatches_to_operation(self):
        # Given 判别键 action=ping
        tool = _EchoOpTool()
        # When run
        result = await tool.run({"action": "ping", "msg": "hello"})
        # Then 分发到 ping 方法
        assert result == {"ok": True, "echo": "hello"}

    @pytest.mark.asyncio
    async def test_given_unknown_operation_when_run_then_raises(self):
        # Given 未知 action
        tool = _EchoOpTool()
        # When run
        with pytest.raises(KeyError):
            await tool.run({"action": "nope"})

    @pytest.mark.asyncio
    async def test_given_no_operation_tool_when_invoke_then_calls_run(self):
        # Given 无 operation 工具覆写 run()
        tool = _NoOpTool()
        # When invoke
        result = await tool.invoke({"x": "value"})
        # Then 调用 run()
        assert result == {"result": "value"}

    @pytest.mark.asyncio
    async def test_given_needs_backend_when_invoke_then_backend_injected(self):
        # Given needs_backend=True 的工具
        tool = _EchoOpTool()
        backend = object()
        # When invoke 携带 backend
        await tool.invoke({"action": "ping"}, backend=backend)
        # Then backend 注入到工具实例
        assert tool.backend is backend
