"""Shared test scaffolding: drive a DagPlan layer-by-layer via execute_layer.

Replaces calls to the removed ``DagExecutor.execute()`` so the layer-orbit
stration (plan_id, Plan/created, Plan/failed|completed bookkeeping) is not
reimplemented in every test file. Production's equivalent loop lives in
``harness/core/scheduler/plan.py`` — this helper mirrors its event shapes.
"""

from __future__ import annotations

from uuid import uuid4

from harness.core.dag_executor import DagExecutor, plan_steps_to_payload
from harness.core.dag_types import StepResult
from harness.models.events import (
    EventType,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
)
from harness.models.plan import DagPlan


async def run_plan_layers(
    dag: DagExecutor, run_id: str, plan: DagPlan
) -> dict[str, StepResult]:
    plan_id = f"plan_{run_id}_{uuid4().hex[:8]}"
    results: dict[str, StepResult] = {}
    layers = plan.topological_sort()

    await dag.store.append_event(
        run_id,
        EventType.PLAN,
        PlanCreatedPayload(
            plan_id=plan_id,
            intent=plan.intent,
            steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
            layer_count=len(layers),
            steps=plan_steps_to_payload(plan),
        ).model_dump(),
    )

    for layer_idx, layer in enumerate(layers):
        ok = await dag.execute_layer(
            run_id, plan, plan_id, layer, layer_idx, layers, results
        )
        if not ok:
            failed = [(sid, r.error or "unknown") for sid, r in results.items() if r.is_failed]
            if failed:
                await dag.store.append_event(
                    run_id,
                    EventType.PLAN,
                    PlanFailedPayload(
                        plan_id=plan_id,
                        completed_steps=sum(1 for r in results.values() if r.step_normal),
                        total_layers=len(layers),
                        final_error=failed[0][1],
                    ).model_dump(),
                )
            return results

    total_completed = sum(1 for r in results.values() if r.step_normal)
    await dag.store.append_event(
        run_id,
        EventType.PLAN,
        PlanCompletedPayload(
            plan_id=plan_id,
            completed_steps=total_completed,
            total_layers=len(layers),
            summary=f"Completed {total_completed}/{len(plan.steps)} steps across {len(layers)} layers",
        ).model_dump(),
    )
    return results
