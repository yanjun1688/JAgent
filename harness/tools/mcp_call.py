from __future__ import annotations

from typing import Any

from harness.models.tools import Guardrail, SideEffect, ToolDefinition

MCP_CALL_DEF = ToolDefinition(
    name="mcp_call",
    description="Call a tool exposed by a connected MCP server to invoke external tools.",
    input_schema={
        "type": "object",
        "properties": {
            "server_name": {
                "type": "string",
                "description": "Name of the MCP server (omit if using the default server)",
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
)


_mcp_sessions: dict[str, Any] = {}


def _get_session(server_name: str | None) -> Any:
    key = server_name or "__default__"
    return _mcp_sessions.get(key)


async def mcp_call_fn(input: dict[str, Any]) -> dict[str, Any]:
    server_name = input.get("server_name")
    tool_name = input["tool_name"]
    arguments = input.get("arguments", {})

    session = _get_session(server_name)
    if session is None:
        return {
            "success": False,
            "error": f"No active MCP session for server '{server_name or 'default'}'. Connect first.",
        }

    try:
        result = await session.call_tool(tool_name, arguments)

        content_parts = []
        if hasattr(result, "content"):
            for item in result.content:
                if hasattr(item, "text"):
                    content_parts.append(item.text)
                elif hasattr(item, "data"):
                    content_parts.append(str(item.data))
                else:
                    content_parts.append(str(item))

        return {"success": True, "content": content_parts}

    except Exception as exc:
        return {"success": False, "error": f"MCP tool '{tool_name}' failed: {exc}"}


async def connect_mcp_server(
    server_name: str,
    command: list[str] | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if command:
        server_params = StdioServerParameters(command=command[0], args=command[1:])
        transport = await stdio_client(server_params).__aenter__()
        session = await ClientSession(transport[0], transport[1]).__aenter__()
        await session.initialize()
    elif url:
        from mcp.client.sse import sse_client
        transport = await sse_client(url).__aenter__()
        session = await ClientSession(transport[0], transport[1]).__aenter__()
        await session.initialize()
    else:
        return {"success": False, "error": "Either command or url must be provided"}

    _mcp_sessions[server_name] = session
    tools_result = await session.list_tools()
    tools_info = [{"name": t.name, "description": t.description} for t in tools_result.tools]
    return {"success": True, "tools": tools_info}


async def disconnect_mcp_server(server_name: str) -> dict[str, Any]:
    session = _mcp_sessions.pop(server_name, None)
    if session:
        try:
            await session.__aexit__(None, None, None)
        except Exception:
            pass
    return {"success": True}
