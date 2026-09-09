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

from harness.models.tools import (
    Guardrail,
    SideEffect,
    ToolScopeTarget,
    unknown_tool_message as canonical_message,
)
from harness.tools.base import BaseTool, operation
from harness.tools.registry import (
    ToolRegistry,
    UnknownToolError,
    unknown_tool_message,
)


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


class _DeleteWithoutGuardrailTool(BaseTool):
    """Regression: SideEffect.DELETE op without the destructive guardrail must
    be rejected at registration — otherwise deletion executes without
    confirmation (DestructiveOpGuardrail is the sole DELETE→confirmation
    translator; the executor never reads side_effects for that decision)."""

    name = "delete_unprotected"
    description = "Declares DELETE side effect but forgets the destructive guardrail"
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    side_effects: list[SideEffect] = []

    @operation("delete", side_effects=[SideEffect.DELETE])
    async def delete(self, input):
        return {"ok": True}


class _DeleteWithGuardrailTool(BaseTool):
    name = "delete_protected"
    description = "Declares DELETE side effect and carries the destructive guardrail"
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    side_effects: list[SideEffect] = []
    guardrails = [Guardrail(guardrail_type="destructive", config={})]

    @operation("delete", side_effects=[SideEffect.DELETE])
    async def delete(self, input):
        return {"ok": True}


class _ToolLevelDeleteWithoutGuardrailTool(BaseTool):
    """Tool-level SideEffect.DELETE (no per-op contracts) without guardrail."""

    name = "tool_level_delete_unprotected"
    description = "Tool-level DELETE side effect without destructive guardrail"
    input_schema = {"type": "object"}
    side_effects = [SideEffect.DELETE]

    async def run(self, input):
        return {"ok": True}


class _ScopeTargetWithoutGuardrailTool(BaseTool):
    """Regression: scope_targets declared but the scope guardrail is missing —
    the whitelist declaration would have no enforcer."""

    name = "scope_unprotected"
    description = "Declares a path scope target but forgets the scope guardrail"
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    side_effects = [SideEffect.WRITE]
    scope_targets = [ToolScopeTarget(kind="path", input_field="path")]

    async def run(self, input):
        return {"ok": True}


class _ScopeTargetWithGuardrailTool(_ScopeTargetWithoutGuardrailTool):
    name = "scope_protected"
    guardrails = [Guardrail(guardrail_type="scope", config={})]


class TestRegistrationSafetyValidation:
    """Fail-closed registration-time checks (trusted boundary): a tool that
    declares a dangerous contract without the matching guardrail must fail to
    register, so "forgot to attach the guardrail" is a startup error rather
    than a silent runtime allow."""

    def test_given_delete_op_without_destructive_guardrail_when_register_then_raises(self):
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="destructive"):
            registry.register_tool(_DeleteWithoutGuardrailTool())

    def test_given_tool_level_delete_without_destructive_guardrail_when_register_then_raises(self):
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="destructive"):
            registry.register_tool(_ToolLevelDeleteWithoutGuardrailTool())

    def test_given_delete_op_with_destructive_guardrail_when_register_then_accepted(self):
        registry = ToolRegistry()
        assert registry.register_tool(_DeleteWithGuardrailTool()) == "delete_protected"

    def test_given_scope_target_without_scope_guardrail_when_register_then_raises(self):
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="scope"):
            registry.register_tool(_ScopeTargetWithoutGuardrailTool())

    def test_given_scope_target_with_scope_guardrail_when_register_then_accepted(self):
        registry = ToolRegistry()
        assert registry.register_tool(_ScopeTargetWithGuardrailTool()) == "scope_protected"

    def test_given_read_only_tool_without_guardrails_when_register_then_accepted(self):
        # Tools with no DELETE side effect and no scope targets need no guardrails.
        registry = ToolRegistry()
        assert registry.register_tool(_PingTool()) == "ping_tool"


class TestToolExistencePrimitive:
    """R7: the two-layer existence primitive — a non-raising lookup that
    returns the canonical fragment, plus a thin fail-fast wrapper. The core
    message fragment must always originate from ``unknown_tool_message``."""

    def test_registered_lookup_returns_def_and_no_error(self):
        registry = ToolRegistry()
        registry.register_tool(_PingTool())
        tool_def, error = registry.tool_def_or_error("ping_tool")
        assert tool_def is not None
        assert tool_def.name == "ping_tool"
        assert error is None

    def test_unknown_lookup_returns_none_and_shared_fragment_without_raising(self):
        registry = ToolRegistry()
        tool_def, error = registry.tool_def_or_error("ghost")
        assert tool_def is None
        # The fragment must come from the single shared source.
        assert error == unknown_tool_message("ghost")

    def test_require_tool_def_raises_shared_error_for_unknown(self):
        registry = ToolRegistry()
        with pytest.raises(UnknownToolError) as exc_info:
            registry.require_tool_def("ghost")
        assert str(exc_info.value) == unknown_tool_message("ghost")
        assert exc_info.value.name == "ghost"

    def test_require_tool_def_returns_def_when_registered(self):
        registry = ToolRegistry()
        registry.register_tool(_PingTool())
        assert registry.require_tool_def("ping_tool").name == "ping_tool"

    def test_registry_reexport_is_same_function_as_models_source(self):
        """The tools.registry export is a compatibility re-export of the single
        canonical function in zero-dependency models.tools — not a second copy.
        Pins identity so a future drift cannot silently reintroduce a duplicate."""
        assert unknown_tool_message is canonical_message

