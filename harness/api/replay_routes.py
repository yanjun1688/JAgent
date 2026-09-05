"""REST API for the Event Replay Inspector -- strictly read-only (GET only).

Every endpoint here is a ``GET`` that reads through the tenant-scoped store
(``api.store``). There are no POST/PATCH/PUT/DELETE handlers, and this module
constructs only :class:`harness.replay.service.ReplayInspectorService`, which
imports no write/execution component. Tenant isolation is inherited from the
middleware + ``ScopedEventStore``: another tenant's run simply reads as empty
and returns 404.

A future rollback/fork capability will be a *separate* router/service (with its
own explicit write path) -- it must not be added here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from harness.api.deps import HarnessAPI, get_hapi
from harness.core.logger import guard_logger
from harness.replay.schemas import (
    ReplayRunMeta,
    ReplayTimelineResponse,
    RunStateView,
    StateDiff,
)
from harness.replay.service import (
    ReplayInspectorService,
    ReplayRunNotFoundError,
    ReplaySeqOutOfRangeError,
)

_log = guard_logger("replay.api")
router = APIRouter(tags=["replay"])


def _service(api: HarnessAPI) -> ReplayInspectorService:
    # Read-only service over the tenant-scoped store. No tracer/write component
    # is wired in this release; the Langfuse link field stays reserved (null).
    return ReplayInspectorService(api.store)


def _not_found(run_id: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "Run not found", "run_id": run_id})


# -- Run metadata --


@router.get("/api/v1/replay/runs/{run_id}/meta", response_model=ReplayRunMeta)
async def replay_run_meta(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    meta = await _service(api).get_run_meta(run_id)
    if meta is None:
        return _not_found(run_id)
    return meta


# -- Event timeline --


@router.get("/api/v1/replay/runs/{run_id}/timeline", response_model=ReplayTimelineResponse)
async def replay_run_timeline(
    run_id: str,
    cursor: int = Query(0, ge=0, description="Event index offset (0 = beginning)"),
    limit: int = Query(200, ge=1, le=1000),
    api: HarnessAPI = Depends(get_hapi),
):
    result = await _service(api).get_timeline(run_id, cursor=cursor, limit=limit)
    if result is None:
        return _not_found(run_id)
    return result


# -- State at a point in time --


@router.get("/api/v1/replay/runs/{run_id}/state", response_model=RunStateView)
async def replay_run_state(
    run_id: str,
    at_seq: int | None = Query(None, ge=1, description="Reconstruct state as-of this seq; omit = latest"),
    api: HarnessAPI = Depends(get_hapi),
):
    try:
        return await _service(api).get_state_at(run_id, at_seq=at_seq)
    except ReplayRunNotFoundError:
        return _not_found(run_id)
    except ReplaySeqOutOfRangeError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


# -- Diff between two points in time --


@router.get("/api/v1/replay/runs/{run_id}/diff", response_model=StateDiff)
async def replay_run_diff(
    run_id: str,
    from_seq: int = Query(..., ge=1, description="Start point (exclusive lower bound)"),
    to_seq: int = Query(..., ge=1, description="End point (inclusive upper bound)"),
    api: HarnessAPI = Depends(get_hapi),
):
    try:
        return await _service(api).get_diff(run_id, from_seq=from_seq, to_seq=to_seq)
    except ReplayRunNotFoundError:
        return _not_found(run_id)
    except ReplaySeqOutOfRangeError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
