from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from harness.analysis.service import AnalysisService
from harness.analysis.schemas import (
    DashboardResponse,
    GuardrailStatsResponse,
    RunAnalysisSummary,
    TimelineResponse,
    ToolStatsResponse,
    ToolTracesResponse,
)
from harness.api.deps import HarnessAPI, get_hapi
from harness.core.logger import guard_logger

_log = guard_logger("analysis.api")
router = APIRouter(tags=["analysis"])


def _service(api: HarnessAPI) -> AnalysisService:
    return AnalysisService(api.store)


# ── Dashboard ──────────────────────────────────────────────


@router.get("/api/v1/analysis/dashboard", response_model=DashboardResponse)
async def analysis_dashboard(
    since: float | None = Query(None, description="Unix timestamp, default 24h ago"),
    until: float | None = Query(None, description="Unix timestamp, default now"),
    api: HarnessAPI = Depends(get_hapi),
):
    s = _service(api)
    if since is None:
        since = time.time() - 86400
    return await s.get_dashboard(since=since, until=until)


# ── Tool Stats ─────────────────────────────────────────────


@router.get("/api/v1/analysis/tools", response_model=ToolStatsResponse)
async def analysis_tool_stats(
    since: float | None = Query(None, description="Unix timestamp, default 24h ago"),
    until: float | None = Query(None, description="Unix timestamp, default now"),
    api: HarnessAPI = Depends(get_hapi),
):
    s = _service(api)
    if since is None:
        since = time.time() - 86400
    return await s.get_tool_stats(since=since, until=until)


# ── Guardrail Stats ────────────────────────────────────────


@router.get("/api/v1/analysis/guardrails", response_model=GuardrailStatsResponse)
async def analysis_guardrail_stats(
    since: float | None = Query(None, description="Unix timestamp, default 24h ago"),
    until: float | None = Query(None, description="Unix timestamp, default now"),
    api: HarnessAPI = Depends(get_hapi),
):
    s = _service(api)
    if since is None:
        since = time.time() - 86400
    return await s.get_guardrail_stats(since=since, until=until)


# ── Run Analysis ───────────────────────────────────────────


@router.get("/api/v1/analysis/runs/{run_id}", response_model=RunAnalysisSummary)
async def analysis_run_detail(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    s = _service(api)
    result = await s.get_run_analysis(run_id)
    if result is None:
        return JSONResponse(status_code=404, content={"error": "Run not found"})
    return result


@router.get("/api/v1/analysis/runs/{run_id}/timeline", response_model=TimelineResponse)
async def analysis_run_timeline(
    run_id: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: int = Query(0, ge=0, description="Starting seq offset, 0 = beginning of time"),
    api: HarnessAPI = Depends(get_hapi),
):
    s = _service(api)
    return await s.get_run_timeline(run_id, limit=limit, cursor=cursor)


@router.get("/api/v1/analysis/runs/{run_id}/tool-traces", response_model=ToolTracesResponse)
async def analysis_run_tool_traces(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    s = _service(api)
    return await s.get_run_tool_traces(run_id)


# ── Operations (future — placeholder) ──────────────────────


@router.post("/api/v1/operations/retry")
async def operation_retry():
    return JSONResponse(
        status_code=501,
        content={"error": "Not Implemented", "message": "Operations layer will be available in a future release."},
    )
