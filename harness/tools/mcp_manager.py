from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from harness.core.logger import agent_logger
from harness.models.mcp_config import MCPConfig, MCPConnectionConfig
from harness.tools.registry import ToolRegistry

_logger = agent_logger("mcp.manager")


@dataclass
class _MCPSession:
    """Holds all context-manager layers for one MCP server connection.

    Close order (reverse of open):
      1. ClientSession  (protocol / initialize)
      2. transport      (stdio or SSE streams + subprocess)
    """

    name: str
    session: ClientSession
    _transport_cm: Any = field(default=None, repr=False)
    _session_cm: Any = field(default=None, repr=False)

    async def close(self) -> None:
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except BaseException:
                _logger.warning("ClientSession cleanup failed for '%s'", self.name)
        if self._transport_cm is not None:
            try:
                await self._transport_cm.__aexit__(None, None, None)
            except BaseException:
                _logger.warning("Transport cleanup failed for '%s'", self.name)


class MCPServerManager:
    """Manages lifecycle of multiple MCP server sessions.

    Responsibilities:
      - Read declarative MCP config (from file / env / dict)
      - Connect / disconnect MCP servers via stdio or SSE
      - Optionally expose MCP tools as first-class ToolRegistry entries
      - Provide session lookup for mcp_call tool
    """

    def __init__(self, config: MCPConfig, registry: ToolRegistry | None = None):
        self.config = config
        self.registry = registry
        self._sessions: dict[str, _MCPSession] = {}

    async def start_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for cfg in self.config.servers:
            if not cfg.enabled:
                _logger.info("MCP server '%s' is disabled, skipping", cfg.name)
                continue
            result = await self.connect_server(cfg)
            results.append(result)
        return results

    async def shutdown_all(self) -> None:
        for name in list(self._sessions):
            try:
                await self.disconnect_server(name)
            except BaseException:
                _logger.warning("Error disconnecting MCP server '%s' during shutdown", name)

    async def connect_server(self, cfg: MCPConnectionConfig) -> dict[str, Any]:
        try:
            if cfg.command:
                mcp_sess = await asyncio.wait_for(
                    self._connect_stdio(cfg),
                    timeout=cfg.timeout_ms / 1000,
                )
            elif cfg.url:
                mcp_sess = await self._connect_sse(cfg)
            else:
                return {"name": cfg.name, "success": False, "error": "Either command or url must be provided"}

            # Normalize: _connect_stdio/sse always returns _MCPSession in prod,
            # but tests may mock it to return a raw ClientSession.
            if not isinstance(mcp_sess, _MCPSession):
                mcp_sess = _MCPSession(
                    name=cfg.name,
                    session=mcp_sess,
                )

            self._sessions[cfg.name] = mcp_sess

            tools_result = await mcp_sess.session.list_tools()
            tools_info = [{
                "name": t.name,
                "description": t.description,
                "inputSchema": getattr(t, "inputSchema", None),
            } for t in tools_result.tools]

            _logger.info(
                "Connected MCP server '%s': %d tools",
                cfg.name, len(tools_info),
            )

            if cfg.auto_register_tools and self.registry is not None:
                self._register_tools(cfg.name, tools_result.tools)

            return {"name": cfg.name, "success": True, "tools": tools_info}

        except asyncio.TimeoutError:
            _logger.warning("Failed to connect MCP server '%s': timeout (%dms)", cfg.name, cfg.timeout_ms)
            return {"name": cfg.name, "success": False, "error": f"Connection timed out after {cfg.timeout_ms}ms"}
        except BaseException as exc:
            msg = str(exc) or type(exc).__name__
            _logger.warning("Failed to connect MCP server '%s': %s", cfg.name, msg)
            return {"name": cfg.name, "success": False, "error": msg}

    async def disconnect_server(self, name: str) -> dict[str, Any]:
        entry = self._sessions.pop(name, None)
        if isinstance(entry, _MCPSession):
            await entry.close()
        _logger.info("Disconnected MCP server '%s'", name)
        return {"name": name, "success": True}

    def get_session(self, name: str) -> ClientSession | None:
        entry = self._sessions.get(name)
        if entry is None:
            return None
        if isinstance(entry, _MCPSession):
            return entry.session
        return entry  # backward compat for tests

    @property
    def server_names(self) -> list[str]:
        return list(self._sessions.keys())

    # ── Private helpers ──────────────────────────────────────────

    async def _connect_stdio(self, cfg: MCPConnectionConfig) -> _MCPSession:
        server_params = StdioServerParameters(
            command=cfg.command[0],
            args=cfg.command[1:],
            env=cfg.environment or None,
        )
        transport_cm = stdio_client(server_params)
        read, write = await transport_cm.__aenter__()
        try:
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            try:
                await session.initialize()
                return _MCPSession(
                    name=cfg.name,
                    session=session,
                    _transport_cm=transport_cm,
                    _session_cm=session_cm,
                )
            except BaseException:
                try:
                    await session_cm.__aexit__(*sys.exc_info())
                except BaseException:
                    pass
                raise
        except BaseException:
            try:
                await transport_cm.__aexit__(*sys.exc_info())
            except BaseException:
                pass
            raise

    async def _connect_sse(self, cfg: MCPConnectionConfig) -> _MCPSession:
        transport_cm = sse_client(url=cfg.url)
        read, write = await transport_cm.__aenter__()
        try:
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            try:
                await session.initialize()
                return _MCPSession(
                    name=cfg.name,
                    session=session,
                    _transport_cm=transport_cm,
                    _session_cm=session_cm,
                )
            except BaseException:
                try:
                    await session_cm.__aexit__(*sys.exc_info())
                except BaseException:
                    pass
                raise
        except BaseException:
            try:
                await transport_cm.__aexit__(*sys.exc_info())
            except BaseException:
                pass
            raise

    def _register_tools(self, server_name: str, tools: list[Any]) -> None:
        from harness.models.tools import Guardrail, SideEffect, ToolDefinition

        for t in tools:
            td = ToolDefinition(
                name=t.name,
                description=f"[{server_name}] {t.description}",
                input_schema=t.inputSchema or {},
                output_schema={},
                idempotency_key_fields=[],
                side_effects=[SideEffect.EXTERNAL],
                guardrails=[Guardrail(guardrail_type="scope", config={})],
                timeout_ms=60000,
            )
            try:
                self.registry.register(td, None)
                _logger.debug("Registered MCP tool '%s' from server '%s'", t.name, server_name)
            except ValueError:
                _logger.debug("Tool '%s' already registered, skipping", t.name)
