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

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harness.api.deps import HarnessAPI, configure_hapi, get_hapi
from harness.api.routes import router as routes_router
from harness.api.ws import router as ws_router

# ── Re-exports for backward compat (tests use these) ──────────
__all__ = ["HarnessAPI", "configure_hapi", "get_hapi", "app"]


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
        yield
    finally:
        await api.store.close()


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
