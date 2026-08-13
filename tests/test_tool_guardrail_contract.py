"""Feature: 契约驱动的 Guardrails（ADR-010 D-04）

行为分层（Given/When/Then）：
  1. 工具声明 scope_targets(domain) → 白名单外 URL 被 scope 拒绝
  2. 工具声明 scope_targets(domain) → 白名单内 URL 通过
  3. OperationContract.side_effects 含 DELETE → destructive 触发确认
  4. OperationContract.requires_confirmation=True → destructive 触发确认
  5. 只读 operation → 不触发确认
"""

from __future__ import annotations


from harness.models.tools import SideEffect, ToolScopeTarget
from harness.tools.base import BaseTool, operation
from harness.tools.guardrails import DestructiveOpGuardrail, ScopeGuardrail


class _DomainTool(BaseTool):
    name = "domain_tool"
    description = "domain"
    input_schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }
    output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    side_effects = [SideEffect.EXTERNAL]
    scope_targets = [ToolScopeTarget(kind="domain", input_field="url")]

    async def run(self, input):
        return {"ok": True}


class _DestructiveTool(BaseTool):
    name = "destructive_tool"
    description = "d"
    input_schema = {
        "type": "object",
        "properties": {"operation": {"type": "string"}, "path": {"type": "string"}},
        "required": ["operation"],
    }
    output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    operation_key = "operation"

    @operation("read", probe_allowed=True)
    async def read(self, input):
        return {"ok": True}

    @operation("delete", side_effects=[SideEffect.DELETE])
    async def delete(self, input):
        return {"ok": True}

    @operation("confirm", requires_confirmation=True)
    async def confirm(self, input):
        return {"ok": True}


class TestScopeContract:
    def test_given_domain_scope_target_when_url_outside_whitelist_then_rejected(self):
        # Given 工具声明 domain scope target（url 字段）
        td = _DomainTool().to_definition()
        # When 检查白名单外的 URL
        result = ScopeGuardrail.check(td, {"url": "http://evil.com"}, {"allowed_domains": ["good.com"]})
        # Then 被 scope 拒绝
        assert result.passed is False
        assert result.guardrail_id == "scope"

    def test_given_domain_scope_target_when_url_in_whitelist_then_passed(self):
        # Given 工具声明 domain scope target
        td = _DomainTool().to_definition()
        # When 检查白名单内的 URL
        result = ScopeGuardrail.check(td, {"url": "http://good.com"}, {"allowed_domains": ["good.com"]})
        # Then 通过
        assert result.passed is True

    def test_given_no_whitelist_then_passed(self):
        # Given 工具声明 domain scope target 但未配置白名单
        td = _DomainTool().to_definition()
        # When 检查任意 URL
        result = ScopeGuardrail.check(td, {"url": "http://any.com"}, {})
        # Then 放行（空白名单 = 不限制）
        assert result.passed is True


class TestDestructiveContract:
    def test_given_delete_side_effect_when_destructive_check_then_triggers_confirmation(self):
        # Given 工具 delete operation 声明 DELETE side effect
        td = _DestructiveTool().to_definition()
        # When destructive 检查 delete
        result = DestructiveOpGuardrail.check(td, {"operation": "delete"}, {})
        # Then 触发确认
        assert result.passed is True
        assert result.triggers_confirmation is True

    def test_given_requires_confirmation_when_destructive_check_then_triggers(self):
        # Given confirm operation 声明 requires_confirmation
        td = _DestructiveTool().to_definition()
        # When destructive 检查 confirm
        result = DestructiveOpGuardrail.check(td, {"operation": "confirm"}, {})
        # Then 触发确认
        assert result.triggers_confirmation is True

    def test_given_read_operation_then_no_confirmation(self):
        # Given read operation 无 DELETE side effect
        td = _DestructiveTool().to_definition()
        # When destructive 检查 read
        result = DestructiveOpGuardrail.check(td, {"operation": "read"}, {})
        # Then 不触发确认
        assert result.triggers_confirmation is False
