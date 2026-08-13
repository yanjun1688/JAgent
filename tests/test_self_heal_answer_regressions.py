"""Regression cases for revised-step deduplication and final-answer evidence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.core.dag_types import ExecState, StepResult
from harness.core.fold import fold_events
from harness.core.scheduler.base import BaseScheduler
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType, ToolCalledPayload, ToolCompletedPayload
from harness.models.plan import DagPlan, DagStep


def test_revision_does_not_restore_semantically_replaced_skipped_steps():
    """A revised UUID/summary step must replace equivalent old SKIPPED steps."""
    root = DagPlan(
        intent="parallel",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "/delay/3"}),
            DagStep(id="s2", tool="http_request", input={"url": "/status/404"}),
            DagStep(id="s3", tool="http_request", input={"url": "/uuid"}, depends_on=["s2"]),
            DagStep(id="s4", tool="http_request", input={"url": "/anything/parallel-summary"}, depends_on=["s1", "s3"]),
        ],
    )
    revised = DagPlan(
        intent="parallel-revised",
        steps=[
            DagStep(id="s2_fix", tool="http_request", input={"url": "/status/500"}),
            DagStep(id="s3_new", tool="http_request", input={"url": "/uuid"}, depends_on=["s1"]),
            DagStep(
                id="s4_new",
                tool="http_request",
                input={"url": "/anything/parallel-summary"},
                depends_on=["s1", "s3_new"],
            ),
        ],
    )
    results = {
        "s1": StepResult(step_id="s1", exec_state=ExecState.COMPLETED),
        "s2": StepResult(step_id="s2", exec_state=ExecState.UNSUCCESSFUL),
        "s3": StepResult(step_id="s3", exec_state=ExecState.SKIPPED),
        "s4": StepResult(step_id="s4", exec_state=ExecState.SKIPPED),
    }
    aliases = {step.id: step.id for step in root.steps}

    merged = PlanningExecutorScheduler._merge_revised_plan(
        root,
        root,
        revised,
        results,
        aliases,
    )

    # s3/s4 are aliased by exact (tool, input) signature to s3_new/s4_new —
    # semantically replaced SKIPPED steps are NOT restored.
    assert aliases["s3"] == "s3_new"
    assert aliases["s4"] == "s4_new"
    # s2_fix changed input so it is NOT signature-matched. But after signature
    # matching it is the UNIQUE remaining replacement and s2 is the UNIQUE
    # remaining ran-and-failed step → the mapping is forced (D12 unambiguous
    # 1:1 binding). SKIPPED steps are never replaced, only ran-and-failed ones.
    assert aliases["s2"] == "s2_fix"
    # The aliased original s2 is dropped: its result is no longer a canonical
    # dependency and the run is evaluated against s2_fix by the completion gate.
    assert [step.id for step in merged.steps] == ["s2_fix", "s3_new", "s4_new"]
    assert "s2" not in results


def test_revision_does_not_positionally_alias_multiple_failed_steps():
    """Ambiguous changed replacements must not be bound by LLM list order."""
    root = DagPlan(
        intent="two failures",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "/a"}),
            DagStep(id="s2", tool="http_request", input={"url": "/b"}),
        ],
    )
    revised = DagPlan(
        intent="revised",
        steps=[
            DagStep(id="r2", tool="http_request", input={"url": "/new-b"}),
            DagStep(id="r1", tool="http_request", input={"url": "/new-a"}),
        ],
    )
    results = {
        "s1": StepResult(step_id="s1", exec_state=ExecState.FAILED),
        "s2": StepResult(step_id="s2", exec_state=ExecState.FAILED),
    }
    aliases = {step.id: step.id for step in root.steps}

    merged = PlanningExecutorScheduler._merge_revised_plan(
        root,
        root,
        revised,
        results,
        aliases,
    )

    assert aliases == {"s1": "s1", "s2": "s2"}
    assert {step.id for step in merged.steps} == {"s1", "s2", "r1", "r2"}


def test_revision_removes_aliased_stale_result():
    """An aliased failure must not remain available as an external dependency."""
    root = DagPlan(
        intent="stale",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "/bad"}),
        ],
    )
    revised = DagPlan(
        intent="fixed",
        steps=[
            DagStep(id="s1_fix", tool="http_request", input={"url": "/good"}),
        ],
    )
    results = {
        "s1": StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL, output={"old": True}),
    }
    aliases = {"s1": "s1"}

    PlanningExecutorScheduler._merge_revised_plan(root, root, revised, results, aliases)

    assert "s1" not in results


@pytest.mark.asyncio
async def test_final_answer_state_retains_pruned_tool_results(store):
    """Answer evidence must retain a completed tool result pruned from context."""
    run_id = "run-answer-digest"
    await store.append_event(run_id, EventType.RUN_STARTED, {"intent": "test"})
    await store.append_event(
        run_id,
        EventType.TOOL_CALLED,
        ToolCalledPayload(tool_call_id="tc-a", tool_name="http_request", input={"url": "/delay/3"}).model_dump(),
    )
    completed_a = await store.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        ToolCompletedPayload(
            tool_call_id="tc-a",
            tool_name="http_request",
            output={"status_code": 200},
            duration_ms=3000,
        ).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.TOOL_CALLED,
        ToolCalledPayload(tool_call_id="tc-b", tool_name="http_request", input={"url": "/uuid"}).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        ToolCompletedPayload(
            tool_call_id="tc-b",
            tool_name="http_request",
            output={"status_code": 200},
            duration_ms=600,
        ).model_dump(),
    )
    await store.append_event(
        run_id,
        EventType.CONTEXT_PRUNED,
        {
            "pruned_event_refs": [completed_a.seq],
            "pruned_token_count": 20,
            "pruned_seq_count": 1,
        },
    )

    normal_state = fold_events(await store.get_events(run_id))
    assert [result.tool_call_id for result in normal_state.tool_results] == ["tc-b"]

    scheduler = SimpleNamespace(store=store)
    authoritative = await BaseScheduler._refresh_authoritative_state(scheduler, run_id)
    assert [result.tool_call_id for result in authoritative.tool_results] == ["tc-a", "tc-b"]
