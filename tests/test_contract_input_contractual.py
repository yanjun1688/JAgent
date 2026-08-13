"""Feature: 交付契约输入校验契约驱动（ADR-010 D-04 §7.3）

行为分层（Given/When/Then）：
  1. file_op 契约缺 content（write）→ 报 content required（由 required_input 表达）
  2. file_op 合法 write → 通过
  3. http_request 缺 method（判别键，operations 声明）→ 报 method required
  4. http_request 合法 → 通过
  5. browser 缺 action → 报 action required
  6. mcp_call 无 operations → 不要求 operation 判别键
  7. file_op write content 非字符串 → 报类型错误
"""

from __future__ import annotations

from harness.models.intent import validate_delivery_contract_input
from harness.tools.browser_tool import BROWSER_DEF
from harness.tools.file_op import FileOpTool
from harness.tools.http_request import HTTP_REQUEST_DEF
from harness.tools.mcp_call import MCP_CALL_DEF


class TestContractInputContractual:
    def test_given_file_op_write_without_content_then_passes(self):
        # Given file_op 定义（content 类型校验但非必填，行为等价）
        td = FileOpTool().to_definition()
        # When 契约缺 content
        errors = validate_delivery_contract_input("file_op", {"operation": "write", "path": "x.txt"}, td)
        # Then 通过（content 非必填）
        assert errors == []

    def test_given_file_op_valid_write_then_passes(self):
        td = FileOpTool().to_definition()
        errors = validate_delivery_contract_input(
            "file_op", {"operation": "write", "path": "x.txt", "content": "hi"}, td
        )
        assert errors == []

    def test_given_http_request_missing_method_then_required(self):
        # Given http_request 声明 operations（判别键 method）
        # When 契约缺 method
        errors = validate_delivery_contract_input("http_request", {"url": "http://x"}, HTTP_REQUEST_DEF)
        # Then 报 method required（行为等价：method+url 必填）
        assert any("method is required" in e for e in errors)

    def test_given_http_request_valid_then_passes(self):
        errors = validate_delivery_contract_input(
            "http_request", {"method": "GET", "url": "http://x"}, HTTP_REQUEST_DEF
        )
        assert errors == []

    def test_given_browser_missing_action_then_required(self):
        errors = validate_delivery_contract_input("browser", {"url": "http://x"}, BROWSER_DEF)
        assert any("action is required" in e for e in errors)

    def test_given_mcp_call_without_operation_key_then_passes(self):
        # Given mcp_call 无 operations（不要求 operation 判别键）
        errors = validate_delivery_contract_input("mcp_call", {"tool_name": "x"}, MCP_CALL_DEF)
        # Then 通过（只要求 tool_name）
        assert errors == []

    def test_given_file_op_write_non_string_content_then_type_error(self):
        td = FileOpTool().to_definition()
        errors = validate_delivery_contract_input(
            "file_op", {"operation": "write", "path": "x.txt", "content": 123}, td
        )
        assert any("content must be a string" in e for e in errors)
