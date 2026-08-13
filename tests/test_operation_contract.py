"""S02 — OperationContract 契约细化（per-operation 工具契约）测试。

覆盖 INDEX 决策 D-01（引用受信化依赖 output_schema/ref_allowed）、C-04
（per-field ref_allowed）、问题五（Tool Contract 粒度）。
"""

from __future__ import annotations

from harness.models.tools import OperationContract, SideEffect, ToolDefinition, resolve_operation_contract
from harness.tools.browser_tool import BROWSER_DEF
from harness.tools.file_op import FILE_OP_DEF
from harness.tools.http_request import HTTP_REQUEST_DEF
from harness.tools.mcp_call import MCP_CALL_DEF


# ── 解析函数 ─────────────────────────────────────────────────────


def test_resolve_file_op_read():
    op = resolve_operation_contract(FILE_OP_DEF, {"operation": "read", "path": "x.txt"})
    assert op is not None
    assert op.operation == "read"
    assert op.side_effects == []
    assert op.probe_allowed is True


def test_resolve_file_op_write():
    op = resolve_operation_contract(FILE_OP_DEF, {"operation": "write", "path": "x.txt"})
    assert op is not None
    assert op.side_effects == [SideEffect.WRITE]
    assert op.probe_allowed is False


def test_resolve_file_op_delete():
    op = resolve_operation_contract(FILE_OP_DEF, {"operation": "delete", "path": "x.txt"})
    assert op is not None
    assert op.side_effects == [SideEffect.DELETE]
    assert op.probe_allowed is False


def test_resolve_file_op_list_read_only():
    op = resolve_operation_contract(FILE_OP_DEF, {"operation": "list", "path": "."})
    assert op is not None
    assert op.side_effects == []
    assert op.probe_allowed is True


def test_resolve_http_get_head_read_only():
    for method in ("GET", "HEAD"):
        op = resolve_operation_contract(HTTP_REQUEST_DEF, {"url": "http://a", "method": method})
        assert op is not None
        assert op.operation == method
        assert op.side_effects == []
        assert op.probe_allowed is True


def test_resolve_http_mutating_methods_external():
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        op = resolve_operation_contract(HTTP_REQUEST_DEF, {"url": "http://a", "method": method})
        assert op is not None
        assert op.side_effects == [SideEffect.EXTERNAL]
        assert op.probe_allowed is False


def test_resolve_http_default_method_is_get():
    # http_request defaults method to GET; operation resolution must preserve
    # the same read-only semantics for probe validation.
    op = resolve_operation_contract(HTTP_REQUEST_DEF, {"url": "http://a"})
    assert op is not None
    assert op.operation == "GET"
    assert op.probe_allowed is True


def test_resolve_browser_actions():
    op = resolve_operation_contract(BROWSER_DEF, {"action": "extract", "selector": "a"})
    assert op is not None
    assert op.side_effects == []
    assert op.probe_allowed is True
    op = resolve_operation_contract(BROWSER_DEF, {"action": "navigate", "url": "http://a"})
    assert op is not None
    assert op.side_effects == [SideEffect.EXTERNAL]
    assert op.probe_allowed is False


def test_unknown_operation_returns_none():
    assert resolve_operation_contract(FILE_OP_DEF, {"operation": "chmod", "path": "x"}) is None
    assert resolve_operation_contract(HTTP_REQUEST_DEF, {"url": "http://a", "method": "TRACE"}) is None


def test_no_operations_declared_returns_none():
    # mcp_call has dynamic tool names — no static operation discriminant.
    assert MCP_CALL_DEF.operations == []
    assert resolve_operation_contract(MCP_CALL_DEF, {"tool_name": "anything"}) is None


# ── ref_allowed_fields（C-04）─────────────────────────────────────


def test_file_op_path_and_content_never_referenceable():
    op = resolve_operation_contract(FILE_OP_DEF, {"operation": "write", "path": "x.txt"})
    assert op is not None
    assert op.ref_allowed("path") is False
    assert op.ref_allowed("content") is False


def test_http_request_url_not_referenceable_body_is():
    op = resolve_operation_contract(HTTP_REQUEST_DEF, {"url": "http://a", "method": "POST"})
    assert op is not None
    assert op.ref_allowed("url") is False
    assert op.ref_allowed("body") is True


def test_unlisted_field_defaults_false():
    op = resolve_operation_contract(HTTP_REQUEST_DEF, {"url": "http://a", "method": "POST"})
    assert op is not None
    assert op.ref_allowed("timeout_ms") is False


# ── Pydantic / 向后兼容 ──────────────────────────────────────────


def test_tool_definition_keeps_tool_level_side_effects():
    assert [s.value for s in FILE_OP_DEF.side_effects] == ["write", "delete"]
    assert [s.value for s in HTTP_REQUEST_DEF.side_effects] == ["external"]


def test_operation_contract_is_pydantic_no_bare_dicts():
    op = OperationContract(operation="read", side_effects=[], probe_allowed=True)
    dumped = op.model_dump()
    assert dumped["operation"] == "read"
    assert dumped["probe_allowed"] is True


def test_tool_def_resolve_operation_method():
    td = ToolDefinition(
        name="t",
        description="d",
        input_schema={"type": "object", "properties": {}},
        side_effects=[SideEffect.WRITE],
        operations=[OperationContract(operation="x", side_effects=[], probe_allowed=True)],
    )
    assert td.resolve_operation({"operation": "x"}).probe_allowed is True
    assert td.resolve_operation({"operation": "y"}) is None
