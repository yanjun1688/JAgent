from __future__ import annotations

from typing import TYPE_CHECKING, Any

from harness.core.logger import agent_logger
from harness.models.tools import Guardrail, SideEffect, SuccessIndicator, ToolDefinition

if TYPE_CHECKING:
    from harness.tools.mcp_manager import MCPServerManager

_logger = agent_logger("mcp.call")

MCP_CALL_DEF = ToolDefinition(
    name="mcp_call",
    description="Call a tool exposed by a connected MCP server to invoke external tools.",
    input_schema={
        "type": "object",
        "properties": {
            "server_name": {
                "type": "string",
                "description": "Name of the MCP server (omit to use the first connected server)",
            },
            "tool_name": {
                "type": "string",
                "description": "Name of the MCP tool to call",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments to pass to the MCP tool",
                "default": {},
            },
        },
        "required": ["tool_name"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "content": {"type": "array"},
            "error": {"type": "string"},
        },
    },
    idempotency_key_fields=["server_name", "tool_name", "arguments"],
    side_effects=[SideEffect.EXTERNAL],
    guardrails=[Guardrail(guardrail_type="scope", config={})],
    timeout_ms=60000,
    success_indicator=SuccessIndicator(field="success", op="eq", value=True),
)

_manager: MCPServerManager | None = None


def set_manager(manager: MCPServerManager) -> None:
    global _manager
    _manager = manager


def get_manager() -> MCPServerManager | None:
    global _manager
    return _manager


async def mcp_call_fn(input: dict[str, Any]) -> dict[str, Any]:
    server_name = input.get("server_name")
    tool_name = input["tool_name"]
    arguments = input.get("arguments", {})

    manager = get_manager()
    if manager is None:
        _logger.warning("mcp_call failed: no manager")
        return {"success": False, "error": "MCP Manager not initialized. No MCP servers configured."}

    if server_name:
        session = manager.get_session(server_name)
        if session is None:
            _logger.warning("mcp_call failed: unknown server '%s'", server_name)
            return {
                "success": False,
                "error": f"No active MCP session for server '{server_name}'.",
            }
    else:
        servers = manager.server_names
        if not servers:
            _logger.warning("mcp_call failed: no sessions")
            return {"success": False, "error": "No active MCP sessions. Configure MCP servers first."}
        session = manager.get_session(servers[0])
        server_name = servers[0]

    _logger.info("mcp_call %s/%s args=%s", server_name, tool_name, str(arguments))

    try:
        available_tools = manager.get_tool_names(server_name)
        if available_tools and tool_name not in available_tools:
            prefixed = f"{server_name}/{tool_name}"
            if "/" not in tool_name and prefixed in available_tools:
                _logger.info("mcp_call corrected tool_name '%s' -> '%s'", tool_name, prefixed)
                tool_name = prefixed
            else:
                _logger.warning("mcp_call unknown tool '%s' on server '%s', available: %s",
                                tool_name, server_name, ", ".join(available_tools[:10]))
                return {
                    "success": False,
                    "error": f"MCP tool '{tool_name}' not found on server '{server_name}'. "
                             f"Available: {', '.join(available_tools[:20])}",
                }

        result = await session.call_tool(tool_name, arguments)

        if getattr(result, "isError", None) is True:
            error_text = ""
            if hasattr(result, "content"):
                for item in result.content:
                    error_text += str(getattr(item, "text", item))
            _logger.warning("mcp_call %s/%s -> MCP error: %s", server_name, tool_name, error_text[:200])
            return {"success": False, "error": f"MCP tool '{tool_name}' error: {error_text}"}

        content_parts = []
        if hasattr(result, "content"):
            for item in result.content:
                text = getattr(item, "text", None)
                data = getattr(item, "data", None)
                if text is not None:
                    content_parts.append(text)
                elif data is not None:
                    content_parts.append(str(data))
                else:
                    content_parts.append(str(item))

        _logger.info("mcp_call %s/%s -> success (%d items)", server_name, tool_name, len(content_parts))
        return {"success": True, "content": content_parts}

    except Exception as exc:
        _logger.warning("mcp_call %s/%s -> failed: %s", server_name, tool_name, exc)
        return {"success": False, "error": f"MCP tool '{tool_name}' failed: {exc}"}


async def connect_mcp_server(
    server_name: str,
    command: list[str] | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    from harness.models.mcp_config import MCPConfig, MCPConnectionConfig
    from harness.tools.mcp_manager import MCPServerManager

    manager = get_manager()
    if manager is None:
        manager = MCPServerManager(MCPConfig())
        set_manager(manager)

    cfg = MCPConnectionConfig(name=server_name, command=command, url=url, enabled=True)
    result = await manager.connect_server(cfg)
    return result


async def disconnect_mcp_server(server_name: str) -> dict[str, Any]:
    manager = get_manager()
    if manager is None:
        return {"success": False, "error": "MCP Manager not initialized"}
    return await manager.disconnect_server(server_name)
