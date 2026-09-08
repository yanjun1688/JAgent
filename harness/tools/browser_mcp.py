"""First-class browser tools backed by playwright-mcp leases (ADR-011).

Each discovered (and policy-approved) playwright-mcp tool becomes a
``BrowserMcpTool`` registered in the ToolRegistry. The tool itself holds no
browser state: it asks the trusted BrowserPool for the current Run's lease,
so cross-Run isolation is enforced by the pool, not by the Agent.
"""

from __future__ import annotations

from typing import Any

from harness.core.logger import agent_logger
from harness.models.tools import Guardrail, SuccessIndicator
from harness.tools.base import BaseTool
from harness.tools.browser_policy import BrowserToolPolicy
from harness.tools.browser_pool import BrowserLeaseUnavailableError, get_pool
from harness.tools.executor import current_run_id

_logger = agent_logger("browser.tool")


class BrowserMcpTool(BaseTool):
    """One playwright-mcp tool (e.g. browser_navigate) as a first-class tool."""

    guardrails = [Guardrail(guardrail_type="scope", config={})]
    timeout_ms = 120000
    success_indicator = SuccessIndicator(field="success", op="eq", value=True)
    max_parallel = 1

    def __init__(self, name: str, description: str, input_schema: dict[str, Any], policy: BrowserToolPolicy) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "content": {"type": "array"},
                "error": {"type": "string"},
            },
        }
        self._policy = policy
        self.side_effects = policy.side_effects
        self.requires_confirmation = policy.requires_confirmation
        # Browser actions are not safely replayable: never idempotency-cache.
        self.idempotency_key_fields: list[str] = []

    async def run(self, input: dict) -> dict[str, Any]:
        run_id = current_run_id.get()
        pool = get_pool()
        if not run_id or pool is None:
            return {
                "success": False,
                "error": "Browser pool is not available (no active run or pool not initialized).",
            }

        try:
            lease = await pool.acquire(run_id)
        except BrowserLeaseUnavailableError as exc:
            _logger.warning("browser lease unavailable run=%s: %s", run_id, exc)
            return {"success": False, "error": str(exc)}

        if self.name not in lease.tool_names:
            return {
                "success": False,
                "error": f"Browser tool '{self.name}' is not exposed by the connected playwright-mcp server.",
            }

        try:
            result = await lease.call_tool(self.name, input or {})
        except Exception as exc:  # trusted boundary: never leak raw exceptions
            _logger.warning("browser tool %s failed run=%s: %s", self.name, run_id, exc)
            return {"success": False, "error": f"Browser tool '{self.name}' failed: {exc}"}

        if getattr(result, "isError", None) is True:
            error_text = ""
            for item in getattr(result, "content", []) or []:
                error_text += str(getattr(item, "text", item))
            return {"success": False, "error": f"Browser tool '{self.name}' error: {error_text}"}

        content_parts: list[Any] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            data = getattr(item, "data", None)
            content_parts.append(text if text is not None else (data if data is not None else str(item)))
        return {"success": True, "content": content_parts}


async def register_browser_tools(registry: Any, pool: Any = None) -> dict[str, Any]:
    """Discover playwright-mcp tools, apply the trusted policy, register them.

    Returns a structured report (registered names / blocked names / error).
    A missing playwright-mcp binary is a soft failure: the server starts
    without browser tools and the report carries install instructions.
    """
    from harness.tools.browser_command import BrowserCommandNotFoundError
    from harness.tools.browser_policy import policy_for
    from harness.tools.browser_pool import BrowserPool

    pool = pool or get_pool()
    if pool is None or not isinstance(pool, BrowserPool):
        return {"success": False, "error": "browser pool not initialized", "registered": [], "blocked": []}

    try:
        tools = await pool.discover_tools()
    except BrowserCommandNotFoundError as exc:
        return {"success": False, "error": str(exc), "registered": [], "blocked": []}
    except Exception as exc:
        _logger.warning("playwright-mcp discovery failed: %s", exc)
        return {"success": False, "error": f"playwright-mcp discovery failed: {exc}", "registered": [], "blocked": []}

    registered: list[str] = []
    blocked: list[dict[str, str]] = []
    for t in tools:
        policy = policy_for(t.name, allow_evaluate=pool.config.allow_evaluate)
        if policy.blocked:
            blocked.append({"name": t.name, "reason": policy.block_reason})
            _logger.info("browser tool blocked by policy: %s (%s)", t.name, policy.block_reason)
            continue
        try:
            registry.register_tool(
                BrowserMcpTool(
                    name=t.name,
                    description=f"[playwright] {t.description or ''}",
                    input_schema=getattr(t, "inputSchema", None) or {},
                    policy=policy,
                )
            )
            registered.append(t.name)
        except ValueError:
            _logger.debug("browser tool '%s' already registered, skipping", t.name)

    _logger.info("browser tools registered=%d blocked=%d", len(registered), len(blocked))
    return {"success": True, "registered": registered, "blocked": blocked}
