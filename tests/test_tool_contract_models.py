"""Feature: 工具契约模型扩展（ADR-010 D-02）

行为分层（Given/When/Then）：
  1. operation_key 声明判别键 → resolve_operation_contract 用它解析
  2. default_operation 声明默认操作 → 无判别键时命中默认
  3. ToolScopeTarget 声明 scope 目标（path/domain/command）
  4. OperationContract.required_input 声明条件必填键
"""

from __future__ import annotations

from harness.models.tools import OperationContract, SideEffect, ToolDefinition, ToolScopeTarget


class TestOperationKeyResolution:
    """Feature: operation_key 判别键解析"""

    def test_given_tool_declares_operation_key_when_resolve_then_uses_that_key(self):
        # Given 一个声明 operation_key="method" 且含 GET/POST 的 ToolDefinition
        td = ToolDefinition(
            name="http_request",
            description="d",
            input_schema={"type": "object"},
            side_effects=[SideEffect.EXTERNAL],
            operation_key="method",
            operations=[
                OperationContract(operation="GET", side_effects=[]),
                OperationContract(operation="POST", side_effects=[SideEffect.EXTERNAL]),
            ],
        )
        # When 解析含 method=GET 的 input
        op = td.resolve_operation({"method": "GET", "url": "http://x"})
        # Then 返回 GET operation
        assert op is not None
        assert op.operation == "GET"

    def test_given_default_operation_key_when_resolve_then_uses_operation(self):
        # Given 默认 operation_key="operation"
        td = ToolDefinition(
            name="file_op",
            description="d",
            input_schema={"type": "object"},
            side_effects=[SideEffect.WRITE],
        )
        # When 解析含 operation=write 的 input
        op = td.resolve_operation({"operation": "write"})
        # Then 无 operations 契约 → None（工具级行为保留）
        assert op is None

    def test_given_default_operation_when_no_discriminant_then_returns_default(self):
        # Given 声明 default_operation="GET" 且 operations 含 GET/POST
        td = ToolDefinition(
            name="http_request",
            description="d",
            input_schema={"type": "object"},
            side_effects=[SideEffect.EXTERNAL],
            operation_key="method",
            default_operation="GET",
            operations=[OperationContract(operation="GET"), OperationContract(operation="POST")],
        )
        # When 解析无 method 的 input
        op = td.resolve_operation({"url": "http://x"})
        # Then 返回 GET（默认）
        assert op is not None
        assert op.operation == "GET"

    def test_given_unknown_discriminant_then_returns_none(self):
        # Given 工具只声明 GET
        td = ToolDefinition(
            name="http_request",
            description="d",
            input_schema={"type": "object"},
            side_effects=[SideEffect.EXTERNAL],
            operation_key="method",
            default_operation="GET",
            operations=[OperationContract(operation="GET")],
        )
        # When 解析 method=DELETE
        op = td.resolve_operation({"method": "DELETE"})
        # Then 无匹配 → None
        assert op is None


class TestToolScopeTarget:
    """Feature: ToolScopeTarget scope 目标声明"""

    def test_given_scope_target_when_construct_then_fields_available(self):
        # Given 声明一个 domain scope target
        target = ToolScopeTarget(kind="domain", input_field="url")
        # Then kind 与 input_field 可访问，config_key 默认为空
        assert target.kind == "domain"
        assert target.input_field == "url"
        assert target.config_key == ""

    def test_given_tool_declares_scope_targets_when_definition_then_attached(self):
        # Given 工具声明 scope_targets
        td = ToolDefinition(
            name="http_request",
            description="d",
            input_schema={"type": "object"},
            side_effects=[SideEffect.EXTERNAL],
            scope_targets=[ToolScopeTarget(kind="domain", input_field="url")],
        )
        # Then 契约携带 scope 目标
        assert len(td.scope_targets) == 1
        assert td.scope_targets[0].kind == "domain"
        assert td.scope_targets[0].input_field == "url"


class TestRequiredInput:
    """Feature: OperationContract.required_input 条件必填"""

    def test_given_operation_required_input_when_construct_then_attached(self):
        # Given write operation 声明 content 必填
        op = OperationContract(operation="write", required_input=["operation", "path", "content"])
        # Then required_input 可访问
        assert op.required_input == ["operation", "path", "content"]

    def test_given_operation_without_required_input_then_defaults_empty(self):
        # Given 未声明 required_input
        op = OperationContract(operation="read")
        # Then 默认为空列表
        assert op.required_input == []
