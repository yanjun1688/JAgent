"""Feature: http_request / mcp_call 迁移为 BaseTool（ADR-010 循环 8）+ 浏览器收敛（ADR-011）

行为分层（Given/When/Then）：
  1. HttpRequestTool → 6 operations、判别键 method、默认 GET、domain scope、共享 client 生命周期
  2. HttpRequestTool dispatch → GET 命中 get 方法
  3. BrowserMcpTool → playwright-mcp 一等工具：策略驱动（只读/确认/黑名单）、max_parallel=1、不做幂等缓存
  4. McpCallTool → 无 operations，needs_mcp_manager 声明，run 使用注入的 manager
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect, ToolScopeTarget
from harness.tools.browser_mcp import BrowserMcpTool
from harness.tools.browser_policy import policy_for
from harness.tools.file_op import FileOpTool
from harness.tools.http_request import HttpRequestTool
from harness.tools.mcp_call import McpCallTool
from harness.tools.registry import ToolRegistry


class TestServeAssembly:
    def test_given_real_mode_tools_when_register_tool_then_static_tools_registered(self):
        # Given real 模式 3 个静态 BaseTool（浏览器工具由 lifespan 动态注册）
        registry = ToolRegistry()
        # When 经 register_tool 注册
        for tool in (FileOpTool(), HttpRequestTool(), McpCallTool()):
            registry.register_tool(tool)
        # Then 3 个静态工具全部注册，且定义与 invoker 齐备（D-07）
        assert set(registry.tool_names) == {"file_op", "http_request", "mcp_call"}
        for name in ("file_op", "http_request", "mcp_call"):
            assert registry.get_tool_def(name) is not None
            assert registry.get_tool_fn(name) is not None


class TestHttpRequestTool:
    def test_given_http_declares_contract_when_to_definition_then_operations_synthesized(self):
        # Given HttpRequestTool 声明
        td = HttpRequestTool().to_definition()
        # When 合成
        ops = {o.operation: o for o in td.operations}
        # Then 6 operations、判别键、默认操作、scope 目标齐备
        assert td.name == "http_request"
        assert td.operation_key == "method"
        assert td.default_operation == "GET"
        assert set(ops) == {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
        assert ops["GET"].probe_allowed is True
        assert ops["GET"].side_effects == []
        assert ops["POST"].side_effects == [SideEffect.EXTERNAL]
        assert td.scope_targets == [ToolScopeTarget(kind="domain", input_field="url")]

    @pytest.mark.asyncio
    async def test_given_http_input_when_invoke_then_dispatches_to_get(self, monkeypatch):
        # Given GET 请求
        tool = HttpRequestTool()
        captured = {}

        async def fake_request(self, input):
            captured["input"] = input
            return {"status_code": 200}

        monkeypatch.setattr(HttpRequestTool, "_do_request", fake_request)
        # When invoke
        result = await tool.invoke({"method": "GET", "url": "http://x"})
        # Then 命中 GET 方法（_do_request 被调用）
        assert result["status_code"] == 200
        assert captured["input"]["url"] == "http://x"


class TestBrowserMcpTool:
    """ADR-011: playwright-mcp 一等工具的契约由受信策略表合成。"""

    def test_given_browser_policy_when_build_tool_then_contract_synthesized(self):
        from harness.models.tools import SideEffect

        policy = policy_for("browser_navigate")
        tool = BrowserMcpTool("browser_navigate", "[playwright] navigate", {}, policy)
        td = tool.to_definition()
        # Then 工具名、外部副作用、串行强制、无幂等缓存
        assert td.name == "browser_navigate"
        assert td.side_effects == [SideEffect.EXTERNAL]
        assert td.max_parallel == 1
        assert td.idempotency_key_fields == []

    def test_given_readonly_tool_when_policy_then_no_side_effects(self):
        from harness.tools.browser_policy import READONLY_TOOLS

        for name in READONLY_TOOLS:
            td = BrowserMcpTool(name, "ro", {}, policy_for(name)).to_definition()
            assert td.side_effects == [], name

    def test_given_unsafe_tools_when_policy_then_blocked(self):
        assert policy_for("browser_run_code_unsafe").blocked is True
        assert policy_for("browser_evaluate").blocked is True
        # 显式放开 evaluate 后仍需人工确认
        allowed = policy_for("browser_evaluate", allow_evaluate=True)
        assert allowed.blocked is False
        assert allowed.requires_confirmation is True

    def test_given_file_upload_when_policy_then_requires_confirmation(self):
        assert policy_for("browser_file_upload").requires_confirmation is True


class TestMcpCallTool:
    def test_given_mcp_declares_contract_when_to_definition_then_contract_synthesized(self):
        td = McpCallTool().to_definition()
        # Given mcp_call 无 operations（工具级行为）
        assert td.name == "mcp_call"
        assert td.operations == []
        assert td.side_effects == [SideEffect.EXTERNAL]
        assert McpCallTool.needs_mcp_manager is True

    @pytest.mark.asyncio
    async def test_given_mcp_manager_injected_when_invoke_then_manager_attached(self, monkeypatch):
        # Given 注入的 mcp_manager
        tool = McpCallTool()
        manager = object()

        async def fake_run(self, input):
            return {"manager_injected": self.mcp_manager is manager}

        monkeypatch.setattr(McpCallTool, "run", fake_run)
        # When invoke 携带 mcp_manager
        result = await tool.invoke({"tool_name": "echo"}, mcp_manager=manager)
        # Then manager 注入到工具实例并被 run 使用
        assert result == {"manager_injected": True}
