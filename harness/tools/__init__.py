from harness.tools.base import BaseTool, current_backend, make_invoker, operation
from harness.tools.browser_tool import BROWSER_DEF, BrowserManager, BrowserTool, browser_fn
from harness.tools.executor import ExecutionStatus, ToolExecutionResult, ToolExecutor
from harness.tools.file_op import FileOpTool
from harness.tools.guardrails import (
    DependencyGuardrail,
    DestructiveOpGuardrail,
    GuardrailResult,
    GuardrailRunner,
    RateLimitGuardrail,
    SchemaGuardrail,
    ScopeGuardrail,
)
from harness.tools.http_request import HTTP_REQUEST_DEF, HttpRequestTool, close_client, http_request_fn
from harness.tools.idempotency import IdempotencyKeyGenerator
from harness.tools.mcp_call import (
    MCP_CALL_DEF,
    McpCallTool,
    McpDynamicTool,
    connect_mcp_server,
    disconnect_mcp_server,
    mcp_call_fn,
)
from harness.tools.registry import ToolRegistry
from harness.tools.retry import RetryRunner
from harness.tools.sandbox import Sandbox, SandboxResult
from harness.tools.semantic import SemanticEvaluator
from harness.tools.skill import Skill


async def close_tools() -> None:
    """ADR-010 D-08: 统一关闭工具层共享资源（http client / browser / mcp）。"""
    from harness.tools.browser_tool import BrowserManager
    from harness.tools.http_request import close_client
    from harness.tools.mcp_call import get_manager

    await close_client()
    await BrowserManager.cleanup()
    manager = get_manager()
    if manager is not None:
        try:
            await manager.shutdown_all()
        except Exception:
            pass

__all__ = [
    "ToolExecutor",
    "ToolExecutionResult",
    "ExecutionStatus",
    "IdempotencyKeyGenerator",
    "GuardrailRunner",
    "GuardrailResult",
    "SchemaGuardrail",
    "ScopeGuardrail",
    "RateLimitGuardrail",
    "DestructiveOpGuardrail",
    "DependencyGuardrail",
    "RetryRunner",
    "Sandbox",
    "SandboxResult",
    "SemanticEvaluator",
    # V0.2
    "ToolRegistry",
    "HTTP_REQUEST_DEF",
    "http_request_fn",
    "close_client",
    "FileOpTool",
    "HttpRequestTool",
    "BrowserTool",
    "McpCallTool",
    "McpDynamicTool",
    "BaseTool",
    "operation",
    "make_invoker",
    "current_backend",
    "close_tools",
    "BROWSER_DEF",
    "browser_fn",
    "BrowserManager",
    "MCP_CALL_DEF",
    "mcp_call_fn",
    "connect_mcp_server",
    "disconnect_mcp_server",
    "Skill",
]
