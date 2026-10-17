from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from harness.core.logger import agent_logger
from harness.core.tenant import safe_tenant_dir_component
from harness.models.mcp_config import MCPConfig, MCPConnectionConfig
from harness.tools.registry import ToolRegistry

_logger = agent_logger("mcp.manager")

_TENANT_SCOPED_SEPARATOR = "@"
_TENANT_DISCOVERY_ID = "__mcp_discovery__"


def _mcp_state_dir() -> Path:
    return Path(os.environ.get("JAGENT_MCP_STATE_DIR", "data/mcp"))


@dataclass
class _MCPSession:
    """Holds all context-manager layers for one MCP server connection.

    Close order (reverse of open):
      1. ClientSession  (protocol / initialize)
      2. transport      (stdio or SSE streams + subprocess)
    """

    name: str
    session: ClientSession
    tool_names: list[str] = field(default_factory=list)
    _transport_cm: Any = field(default=None, repr=False)
    _session_cm: Any = field(default=None, repr=False)

    async def close(self) -> None:
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except BaseException:
                _logger.debug("ClientSession cleanup failed for '%s'", self.name)
        if self._transport_cm is not None:
            try:
                await self._transport_cm.__aexit__(None, None, None)
            except BaseException:
                _logger.debug("Transport cleanup failed for '%s'", self.name)


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
        self._tenant_connect_lock = asyncio.Lock()

    async def start_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for cfg in self.config.servers:
            if not cfg.enabled:
                _logger.info("MCP server '%s' is disabled, skipping", cfg.name)
                continue
            if cfg.tenant_scoped:
                _logger.info(
                    "MCP server '%s' is tenant-scoped; connecting lazily per tenant",
                    cfg.name,
                )
                # Discover the tool surface for mcp_call's prompt, then close
                # the transient session: tenant data must never live in a
                # process shared by all tenants.
                discovery = await self.connect_server(cfg, tenant_id=_TENANT_DISCOVERY_ID)
                if discovery.get("success"):
                    await self.disconnect_server(
                        f"{cfg.name}{_TENANT_SCOPED_SEPARATOR}{_TENANT_DISCOVERY_ID}"
                    )
                results.append(discovery)
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

    async def connect_server(
        self,
        cfg: MCPConnectionConfig,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        session_name = cfg.name
        effective_cfg = cfg
        try:
            if cfg.tenant_scoped:
                if not tenant_id:
                    return {
                        "name": cfg.name,
                        "success": False,
                        "error": f"MCP server '{cfg.name}' is tenant-scoped and requires a tenant context",
                    }
                effective_cfg = cfg.model_copy(
                    update={
                        "environment": self._tenant_environment(cfg, tenant_id),
                        "name": f"{cfg.name}{_TENANT_SCOPED_SEPARATOR}{tenant_id}",
                    }
                )
                session_name = effective_cfg.name

            if effective_cfg.command:
                mcp_sess = await asyncio.wait_for(
                    self._connect_stdio(effective_cfg),
                    timeout=effective_cfg.timeout_ms / 1000,
                )
            elif effective_cfg.url:
                mcp_sess = await self._connect_sse(effective_cfg)
            else:
                return {"name": cfg.name, "success": False, "error": "Either command or url must be provided"}

            # Normalize: _connect_stdio/sse always returns _MCPSession in prod,
            # but tests may mock it to return a raw ClientSession.
            if not isinstance(mcp_sess, _MCPSession):
                mcp_sess = _MCPSession(
                    name=session_name,
                    session=mcp_sess,
                )

            self._sessions[session_name] = mcp_sess

            tools_result = await mcp_sess.session.list_tools()
            tools_info = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": getattr(t, "inputSchema", None),
                }
                for t in tools_result.tools
            ]

            mcp_sess.tool_names = [t.name for t in tools_result.tools]

            _logger.debug(
                "Connected MCP server '%s': %d tools",
                session_name,
                len(tools_info),
            )

            if effective_cfg.auto_register_tools and self.registry is not None and not effective_cfg.tenant_scoped:
                self._register_tools(session_name, tools_result.tools)

            return {"name": cfg.name, "success": True, "tools": tools_info}

        except asyncio.TimeoutError:
            _logger.warning("Failed to connect MCP server '%s': timeout (%dms)", session_name, effective_cfg.timeout_ms)
            return {
                "name": cfg.name,
                "success": False,
                "error": f"Connection timed out after {effective_cfg.timeout_ms}ms",
            }
        except BaseException as exc:
            msg = str(exc) or type(exc).__name__
            _logger.warning("Failed to connect MCP server '%s': %s", session_name, msg)
            return {"name": cfg.name, "success": False, "error": msg}

    async def disconnect_server(self, name: str) -> dict[str, Any]:
        keys = [
            key
            for key in self._sessions
            if key == name or key.startswith(f"{name}{_TENANT_SCOPED_SEPARATOR}")
        ]
        for key in keys:
            entry = self._sessions.pop(key, None)
            if isinstance(entry, _MCPSession):
                await entry.close()
        _logger.debug("Disconnected MCP server '%s'", name)
        return {"name": name, "success": True}

    def get_session(self, name: str) -> ClientSession | None:
        entry = self._sessions.get(name)
        if entry is None:
            return None
        if isinstance(entry, _MCPSession):
            return entry.session
        return entry  # backward compat for tests

    def get_tool_names(self, name: str) -> list[str]:
        entry = self._sessions.get(name)
        if entry is None:
            return []
        if isinstance(entry, _MCPSession):
            return entry.tool_names
        return []

    @property
    def server_names(self) -> list[str]:
        return [name for name in self._sessions if _TENANT_SCOPED_SEPARATOR not in name]

    def is_tenant_scoped(self, name: str) -> bool:
        cfg = self._find_config(name)
        return cfg is not None and cfg.tenant_scoped

    def available_server_names(self, tenant_id: str | None) -> list[str]:
        names: list[str] = []
        for cfg in self.config.servers:
            if not cfg.enabled:
                continue
            if cfg.tenant_scoped and not tenant_id:
                continue
            names.append(cfg.name)
        for name in self.server_names:
            if name not in names:
                names.append(name)
        return names

    async def get_session_for_tenant(self, name: str, tenant_id: str | None) -> ClientSession | None:
        entry = self._sessions.get(name)
        if entry is not None:
            return entry.session if isinstance(entry, _MCPSession) else entry

        cfg = self._find_config(name)
        if cfg is None or not cfg.tenant_scoped or not tenant_id:
            return None

        key = f"{name}{_TENANT_SCOPED_SEPARATOR}{tenant_id}"
        entry = self._sessions.get(key)
        if entry is None:
            async with self._tenant_connect_lock:
                entry = self._sessions.get(key)
                if entry is None:
                    result = await self.connect_server(cfg, tenant_id=tenant_id)
                    if not result.get("success"):
                        return None
                    entry = self._sessions.get(key)
        if entry is None:
            return None
        return entry.session if isinstance(entry, _MCPSession) else entry

    def get_tool_names_for_tenant(self, name: str, tenant_id: str | None) -> list[str]:
        entry = self._sessions.get(name)
        if entry is None and tenant_id:
            entry = self._sessions.get(f"{name}{_TENANT_SCOPED_SEPARATOR}{tenant_id}")
        if isinstance(entry, _MCPSession):
            return entry.tool_names
        return []

    # ── Private helpers ──────────────────────────────────────────

    def _find_config(self, name: str) -> MCPConnectionConfig | None:
        return next((cfg for cfg in self.config.servers if cfg.name == name), None)

    def _tenant_environment(self, cfg: MCPConnectionConfig, tenant_id: str) -> dict[str, str]:
        if not any("{tenant_state_dir}" in value for value in cfg.environment.values()):
            raise ValueError(
                f"MCP server '{cfg.name}' is tenant-scoped but its environment does not "
                "reference {tenant_state_dir}"
            )
        tenant_dir = _mcp_state_dir() / safe_tenant_dir_component(tenant_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
        return {
            key: value.replace("{tenant_id}", tenant_id).replace("{tenant_state_dir}", str(tenant_dir))
            for key, value in cfg.environment.items()
        }

    async def _connect_stdio(self, cfg: MCPConnectionConfig) -> _MCPSession:
        from harness.tools.browser_command import normalize_command

        command = normalize_command(list(cfg.command or []))
        server_params = StdioServerParameters(
            command=command[0],
            args=command[1:],
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
        if cfg.url is None:
            raise ValueError(f"MCP SSE server '{cfg.name}' configured without a url")
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
        # ADR-010 D-05: 动态工具构造 BaseTool 实例走 register_tool（invoker 永不 None）。
        registry = self.registry
        if registry is None:
            raise RuntimeError("MCPServerManager has no tool registry")
        from harness.tools.mcp_call import McpDynamicTool

        for t in tools:
            try:
                registry.register_tool(
                    McpDynamicTool(
                        name=t.name,
                        description=f"[{server_name}] {t.description}",
                        input_schema=t.inputSchema or {},
                        server_name=server_name,
                    )
                )
                _logger.debug("Registered MCP tool '%s' from server '%s'", t.name, server_name)
            except ValueError:
                _logger.debug("Tool '%s' already registered, skipping", t.name)
