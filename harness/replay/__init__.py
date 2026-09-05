"""Event Replay Inspector (time-travel debugger) -- strictly read-only.

This package reconstructs historical system state from the append-only event
stream and exposes it for inspection. It is deliberately read-only:

  - ``projection`` -- pure functions (events -> state/view/diff), built on the
    canonical ``fold_events``. This is the reuse seam for a future rollback /
    fork-from-history write path.
  - ``schemas``    -- Pydantic v2 OpenAPI wire shapes (frontend types generated).
  - ``service``    -- application service; reads only through the tenant-scoped
    store; never imports a write/execution component.

See JAgent-docs/Dev/REPLAY_INSPECTOR_v1.0.md.
"""

from __future__ import annotations

from harness.replay.schemas import (
    ErrorChangeView,
    GuardrailBlockView,
    PendingConfirmationView,
    PlanStepView,
    PlanView,
    ReplayRunMeta,
    ReplayTimelineEvent,
    ReplayTimelineResponse,
    RunStateView,
    StateDiff,
    StatusChangeView,
    StepChangeView,
    ToolResultChangeView,
    ToolResultView,
)
from harness.replay.service import (
    ReplayInspectorService,
    ReplayRunNotFoundError,
    ReplaySeqOutOfRangeError,
)

__all__ = [
    "ReplayInspectorService",
    "ReplayRunNotFoundError",
    "ReplaySeqOutOfRangeError",
    "ReplayRunMeta",
    "ReplayTimelineEvent",
    "ReplayTimelineResponse",
    "RunStateView",
    "PlanView",
    "PlanStepView",
    "ToolResultView",
    "GuardrailBlockView",
    "PendingConfirmationView",
    "StateDiff",
    "StatusChangeView",
    "StepChangeView",
    "ToolResultChangeView",
    "ErrorChangeView",
]
