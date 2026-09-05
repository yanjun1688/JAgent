"""Pure state-reconstruction & projection for the Event Replay Inspector.

Architectural rules (see REPLAY_INSPECTOR_v1.0.md):

1. **State has a single source.** ``reconstruct_state`` never re-derives state;
   it slices the event stream and calls the existing, canonical
   :func:`harness.core.fold.fold_events`. Any future "rollback / fork from
   history" capability must reuse this function to obtain the historical
   ``RunState`` — it is deliberately a pure, caller-agnostic seam
   (``events -> RunState``), not coupled to "display in the UI".

2. **No I/O.** This module imports only the fold function, event models and
   the replay wire-schemas. It has no store, no tenant, no network, and never
   imports a write/execution component (scheduler, tool executor, monitoring,
   lifecycle). That keeps the read path — and the future write path that will
   sit beside it — statically auditable.

The view/diff helpers additionally scan the (already-folded) event slice to
surface structured fields that ``fold`` intentionally flattens into strings
(notably ``guardrail_id`` / ``reason`` for GuardrailTriggered). State itself
is always taken from the folded ``RunState``.
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.core.fold import RunState, fold_events
from harness.models.events import Event, EventType, GuardrailTriggeredPayload
from harness.replay.schemas import (
    ErrorChangeView,
    GuardrailBlockView,
    PendingConfirmationView,
    PlanStepView,
    PlanView,
    ReplayTimelineEvent,
    RunStateView,
    StateDiff,
    StatusChangeView,
    StepChangeView,
    ToolResultChangeView,
    ToolResultView,
)

_TERMINAL_TYPES = {EventType.RUN_COMPLETED, EventType.RUN_FAILED}


# ── The reconstruction seam (pure; reused by a future rollback path) ──


def reconstruct_state(events: Sequence[Event], at_seq: int | None = None) -> RunState:
    """Reconstruct the canonical ``RunState`` as-of ``at_seq``.

    Pure function: takes a seq-ascending event list for a single run, returns
    the folded state. ``at_seq=None`` (or >= the last seq) folds the whole
    stream. The state is produced *only* by :func:`fold_events` — there is no
    parallel/derived state logic here.

    Raises:
        ValueError: on an empty stream, or when ``at_seq`` is < the first
            event's seq (no state exists before the run started).
    """
    if not events:
        raise ValueError("Cannot reconstruct state from an empty event list")
    first_seq = events[0].seq
    if at_seq is not None and at_seq < first_seq:
        raise ValueError(f"at_seq={at_seq} is before the first event (seq={first_seq}); no state exists yet")

    if at_seq is None:
        sliced = list(events)
    else:
        sliced = [e for e in events if e.seq <= at_seq]
    return fold_events(sliced)


# ── Timeline projection ──────────────────────────────────────────


def project_timeline_event(event: Event) -> ReplayTimelineEvent:
    """Project a raw event into a compact timeline row."""
    p = event.payload or {}
    return ReplayTimelineEvent(
        seq=event.seq,
        event_type=event.event_type.value,
        created_at=event.created_at,
        payload=p,
        tool_name=p.get("tool_name"),
        tool_call_id=p.get("tool_call_id"),
        step_id=p.get("step_id"),
        is_terminal=event.event_type in _TERMINAL_TYPES,
    )


# ── State-at-a-point projection ──────────────────────────────────


def _guardrail_blocks(events: Sequence[Event]) -> list[GuardrailBlockView]:
    blocks: list[GuardrailBlockView] = []
    for e in events:
        if e.event_type is not EventType.GUARDRAIL_TRIGGERED:
            continue
        p = GuardrailTriggeredPayload(**e.payload)
        blocks.append(
            GuardrailBlockView(
                guardrail_id=p.guardrail_id,
                reason=p.reason,
                event_seq=e.seq,
                tool_call_id=p.tool_call_id,
                tool_name=p.tool_name,
                step_id=p.step_id,
            )
        )
    return blocks


def _pending_confirmations(state: RunState, events: Sequence[Event]) -> list[PendingConfirmationView]:
    # Fold carries the pending confirmation payloads; attach the originating
    # event seq by scanning the slice for the matching CONFIRMATION_REQUESTED.
    seq_by_id: dict[str, int] = {}
    for e in events:
        if e.event_type is EventType.CONFIRMATION_REQUESTED:
            seq_by_id[e.payload.get("confirmation_id", "")] = e.seq
    views: list[PendingConfirmationView] = []
    for c in state.pending_confirmations:
        views.append(
            PendingConfirmationView(
                confirmation_id=c.confirmation_id,
                tool_name=c.tool_name,
                risk_level=c.risk_level,
                event_seq=seq_by_id.get(c.confirmation_id, 0),
            )
        )
    return views


def _plan_view(state: RunState) -> PlanView | None:
    plan = state.latest_plan
    if not plan:
        return None
    steps = [
        PlanStepView(
            step_id=s.get("step_id", ""),
            status=s.get("status", "pending"),
            tool_name=s.get("tool_name"),
            output_summary=s.get("output_summary"),
            error=s.get("error"),
            reason=s.get("reason"),
            tool_call_id=s.get("tool_call_id"),
        )
        for s in plan.get("steps", [])
    ]
    return PlanView(
        plan_id=plan.get("plan_id"),
        intent=plan.get("intent", state.intent),
        status=plan.get("status"),
        summary=plan.get("summary"),
        final_error=plan.get("final_error"),
        steps=steps,
    )


def project_state_view(events: Sequence[Event], at_seq: int, latest_seq: int) -> RunStateView:
    """Project the folded state as-of ``at_seq`` into a stable read view.

    ``events`` is the run's full event stream (seq-ascending); the function
    reconstructs state internally via :func:`reconstruct_state` (fold-only).
    """
    state = reconstruct_state(events, at_seq=at_seq)
    sliced = [e for e in events if e.seq <= at_seq]

    tool_results = [
        ToolResultView(
            tool_call_id=tr.tool_call_id,
            tool_name=tr.tool_name,
            status=tr.status.value,
            output=tr.output,
            error=tr.error,
            duration_ms=tr.duration_ms,
            event_seq=tr.event_seq,
        )
        for tr in state.tool_results
    ]

    completion_summary = state.summary if isinstance(state.summary, str) else None

    return RunStateView(
        run_id=state.run_id,
        at_seq=state.seq,
        latest_seq=latest_seq,
        is_latest=state.seq >= latest_seq,
        status=state.status.value,
        intent=state.intent,
        last_error=state.last_error,
        user_facing_message=state.user_facing_message,
        pause_reason=state.pause_reason,
        completion_summary=completion_summary,
        completion_evidence=dict(state.completion_evidence),
        plan=_plan_view(state),
        tool_results=tool_results,
        guardrail_blocks=_guardrail_blocks(sliced),
        pending_confirmations=_pending_confirmations(state, sliced),
        thought_count=len(state.thought_history),
        orphaned=state.orphaned,
        workspace_id=state.workspace_id,
        conversation_id=state.conversation_id,
    )


# ── Diff projection ──────────────────────────────────────────────


def _step_status_map(state: RunState) -> dict[str, str]:
    if not state.latest_plan:
        return {}
    return {s.get("step_id", ""): s.get("status", "pending") for s in state.latest_plan.get("steps", [])}


def _step_error_map(state: RunState) -> dict[str, str | None]:
    if not state.latest_plan:
        return {}
    return {s.get("step_id", ""): s.get("error") for s in state.latest_plan.get("steps", [])}


def diff_states(events: Sequence[Event], from_seq: int, to_seq: int) -> StateDiff:
    """Structured diff between state@from_seq and state@to_seq.

    Both states are reconstructed from the same event stream via
    :func:`reconstruct_state` (fold-only). The window is ``(from_seq, to_seq]``.
    """
    if from_seq > to_seq:
        raise ValueError(f"from_seq ({from_seq}) must be <= to_seq ({to_seq})")

    before = reconstruct_state(events, at_seq=from_seq)
    after = reconstruct_state(events, at_seq=to_seq)
    in_range = [e for e in events if from_seq < e.seq <= to_seq]

    # ── Run status transition (prominent) ──
    status_change = None
    if before.status is not after.status:
        status_change = StatusChangeView(from_status=before.status.value, to_status=after.status.value)

    # ── Step status changes (prominent) ──
    before_steps = _step_status_map(before)
    after_steps = _step_status_map(after)
    after_errors = _step_error_map(after)
    steps_changed: list[StepChangeView] = []
    for step_id, to_status in after_steps.items():
        from_status = before_steps.get(step_id)
        if from_status != to_status:
            steps_changed.append(
                StepChangeView(
                    step_id=step_id,
                    from_status=from_status,
                    to_status=to_status,
                    error=after_errors.get(step_id) if to_status == "failed" else None,
                )
            )

    # ── Tool results that appeared in the window ──
    before_result_ids = {tr.tool_call_id for tr in before.tool_results}
    tool_results_added = [
        ToolResultChangeView(
            tool_call_id=tr.tool_call_id,
            tool_name=tr.tool_name,
            status=tr.status.value,
            event_seq=tr.event_seq,
            error=tr.error,
        )
        for tr in after.tool_results
        if tr.tool_call_id not in before_result_ids
    ]

    # ── Guardrails that fired in the window ──
    guardrails_triggered = _guardrail_blocks(in_range)

    # ── Error appearance / change ──
    error_change = None
    if before.last_error != after.last_error:
        error_change = ErrorChangeView(from_error=before.last_error, to_error=after.last_error)

    return StateDiff(
        run_id=after.run_id,
        from_seq=from_seq,
        to_seq=to_seq,
        status_change=status_change,
        steps_changed=steps_changed,
        tool_results_added=tool_results_added,
        guardrails_triggered=guardrails_triggered,
        error_change=error_change,
        events_in_range=[project_timeline_event(e) for e in in_range],
    )


__all__ = [
    "reconstruct_state",
    "project_state_view",
    "project_timeline_event",
    "diff_states",
]
