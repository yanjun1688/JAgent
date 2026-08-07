"""FastAPI application assembly for Harness REST + WebSocket API.

Module structure (split by concern, not by file count):
  schemas.py — request/response Pydantic models (OpenAPI shape)
  deps.py    — HarnessAPI container + FastAPI Depends() helpers
  routes.py  — all REST endpoint handlers
  ws.py      — WebSocket event streaming endpoint
  app.py     — assembly (this file): lifespan, CORS, include routers

Dependency injection via FastAPI Depends():
  - All endpoints receive HarnessAPI through get_hapi() dependency
  - Tests inject mock instances via app.dependency_overrides[get_hapi]
  - Production: configure_hapi() sets the instance before server start
  - Lifespan initializes the EventStore in production; no-op when unconfigured (tests)
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harness.api.analysis_routes import router as analysis_router
from harness.api.deps import HarnessAPI, configure_hapi, get_hapi
from harness.api.query import router as query_router
from harness.api.routes import router as routes_router
from harness.api.ws import router as ws_router
from harness.core.lifecycle import mark_orphans
from harness.models.mcp_config import MCPConfig
from harness.tools.http_request import close_client as close_http_client
from harness.tools.mcp_call import set_manager as set_mcp_manager
from harness.tools.mcp_manager import MCPServerManager

# ── Re-exports for backward compat (tests use these) ──────────
__all__ = ["HarnessAPI", "configure_hapi", "get_hapi", "app"]

_logger = logging.getLogger("harness.api.app")


# ── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        api = get_hapi()
    except RuntimeError:
        yield
        return
    await api.store.initialize()

    try:
        await mark_orphans(api.store)
    except Exception as exc:
        _logger.exception("Failed to mark orphan runs: %s", exc)

    # ── MCP servers ───────────────────────────────────────────
    mcp_config = MCPConfig.from_env()
    mcp_manager = MCPServerManager(mcp_config, registry=api.registry)
    results = await mcp_manager.start_all()
    set_mcp_manager(mcp_manager)
    api.mcp_manager = mcp_manager
    for r in results:
        if r["success"]:
            _logger.info("MCP '%s' connected: %d tools", r["name"], len(r.get("tools", [])))
        else:
            _logger.warning("MCP '%s' connect failed: %s", r["name"], r.get("error"))

    # ── Enrich mcp_call tool description with available MCP tools ──
    mcp_tool_lines = []
    for r in results:
        if not r["success"]:
            continue
        sn = r["name"]
        for t in r.get("tools", []):
            schema = (t.get("inputSchema") or {})
            params = ", ".join(schema.get("properties", {}).keys()) if schema.get("properties") else ""
            mcp_tool_lines.append(f"  - {sn}/{t['name']}({params}): {t.get('description', '')}")

    if mcp_tool_lines:
        from harness.tools.mcp_call import MCP_CALL_DEF
        MCP_CALL_DEF.description += "\n\nAvailable MCP servers and tools:\n" + "\n".join(mcp_tool_lines)
        _logger.info("MCP discovery: %d tools available via mcp_call", len(mcp_tool_lines))

    try:
        yield
    except asyncio.CancelledError:
        _logger.warning("Lifespan cancelled during shutdown")
        raise
    finally:
        try:
            await mcp_manager.shutdown_all()
        except asyncio.CancelledError:
            _logger.warning("MCP shutdown interrupted")
        try:
            await api.store.close()
        except asyncio.CancelledError:
            _logger.warning("Store close interrupted during shutdown")
        try:
            await close_http_client()
        except asyncio.CancelledError:
            _logger.warning("HTTP client close interrupted during shutdown")


# ── App assembly ──────────────────────────────────────────────

app = FastAPI(
    title="Harness API",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_router)
app.include_router(ws_router)
app.include_router(analysis_router)
app.include_router(query_router)
