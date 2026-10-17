"""R6: single trusted seam for recording plan-layer rejections.

Every trusted rejection of a non-trusted planner output is recorded through
:func:`record_plan_rejection` so call sites cannot restate the event shape.
The rejection content is always a trusted verdict, never agent-authored.
"""

from __future__ import annotations

from typing import Literal

from harness.core.logger import agent_logger, trace_event
from harness.models.events import EventType, PlanRejectedPayload
from harness.storage import EventStoreLike

_log = agent_logger("planner.reject")

RejectionStage = Literal["plan", "revise", "merge", "degenerate", "invariant"]


async def record_plan_rejection(
    store: EventStoreLike | None,
    run_id: str | None,
    *,
    stage: RejectionStage,
    attempt: int,
    reasons: list[str],
    recovered: bool,
) -> None:
    """Append a ``Diagnostic/plan_rejected`` event for a trusted plan rejection.

    Safe to call from paths assembled without a store or run_id (unit tests,
    scripts): it degrades to a warning log instead of raising, so trusted
    validation is never blocked by observability plumbing. ``attempt`` is the
    in-call try number; numbering restarts per stage within one run.
    """
    trace_event(
        run_id,
        "HEAL-REJECT",
        stage=stage,
        attempt=attempt,
        recovered=recovered,
        reasons="; ".join(reasons),
    )
    if store is None or not run_id:
        _log.warning(
            "[reject] stage=%s attempt=%d recovered=%s (not recorded): %s",
            stage,
            attempt,
            recovered,
            "; ".join(reasons),
        )
        return
    payload = PlanRejectedPayload(
        stage=stage,
        attempt=attempt,
        reasons=reasons,
        recovered=recovered,
    )
    await store.append_event(
        run_id,
        EventType.DIAGNOSTIC,
        payload.model_dump(),
    )
