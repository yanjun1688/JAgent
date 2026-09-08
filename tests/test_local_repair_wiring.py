"""v3.4 (F-6): bounded step-local repair — scheduler wiring.

The pure budget/validation core lives in ``harness/core/recovery.py`` (see
``test_recovery_core.py``). This module covers the L3 wiring that runs the
bounded local think-act loop BEFORE a global revise, and asserts the
trusted-boundary invariants:

* default-off: with ``SchedulerConfig.local_repair_enabled=False`` the
  scheduler never writes STEP_LOCAL_REPAIR_* events and never calls the
  planner's local-repair proposal path;
* only steps that failed with a ``step_repair`` tier (non-transient,
  non-contract-bound) are candidates;
* an accepted proposal replaces the failing step action in a patched plan and
  the caller re-runs the DAG (no global PlanRevised emitted);
* rejected proposals (mutating tool, not in read-only whitelist, out of
  budget) never mutate the plan and fall through to the global-revise path.
"""

from __future__ import annotations

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import ExecState, StepResult
from harness.core.llm_client import MockLLMClient
from harness.core.planner import Planner
from harness.core.recovery import LocalRepairProposal, RecoveryBudget
from harness.core.scheduler.base import SchedulerConfig
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType
from harness.models.plan import DagPlan, DagStep
from harness.models.intent import DeliveryContract
from harness.models.tools import SideEffect, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry


def _tool_def(name: str, *, side_effects: list[SideEffect] | None = None, ro_ops: list[str] | None = None) -> ToolDefinition:
    operations = []
    for op in ro_ops or []:
        operations.append(
            {
                "operation": op,
                "input_schema": {},
                "side_effects": [],
                "required_input": [],
            }
        )
    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        side_effects=side_effects or [],
        operations=operations,
    )


async def _make_scheduler(
    store: EventStore, *, tool_defs: list[ToolDefinition], config: SchedulerConfig | None = None
) -> PlanningExecutorScheduler:
    executor = ToolExecutor(store)
    registry = ToolRegistry()
    for td in tool_defs:
        registry._register(td, lambda _i, _td=td: {"ok": True, "tool": _td.name})
    dag = DagExecutor(executor, store, registry)
    planner = Planner(MockLLMClient(responses=[]), registry, store)
    return PlanningExecutorScheduler(
        store,
        executor,
        planner,
        dag,
        tool_defs,
        {},
        config=config or SchedulerConfig(local_repair_enabled=True, local_repair_allowed_tools=("http_request", "fetch_output")),
    )


def _failing_result(sid: str, tool: str, error: str, state: ExecState = ExecState.FAILED) -> tuple[DagStep, StepResult]:
    return DagStep(id=sid, tool=tool, input={}), StepResult(step_id=sid, exec_state=state, error=error, tool_call_id=None)


# ── candidate selection (trusted filter) ──────────────────────


class TestLocalRepairCandidates:
    def test_only_non_transient_failed_steps_are_candidates(self):
        sched = PlanningExecutorScheduler.__new__(PlanningExecutorScheduler)
        sched.tool_defs = []
        step1, r1 = _failing_result("s1", "browser", "browser unavailable: asyncio not supported")
        step2, r2 = _failing_result("s2", "http_request", "connection reset by peer")
        step3, r3 = _failing_result("s3", "http_request", "403 Forbidden: read requires authentication", state=ExecState.UNSUCCESSFUL)
        step4, r4 = _failing_result("s4", "http_request", "timeout after 30s")
        plan = DagPlan(intent="t", steps=[step1, step2, step3, step4])
        results = {"s1": r1, "s2": r2, "s3": r3, "s4": r4}
        candidates = [s.id for s in sched._local_repair_candidates(plan, results, contracts=[])]
        # browser env failure → step_repair (candidate); transient → excluded
        assert "s1" in candidates
        assert "s2" not in candidates  # transient → tool-retry tier
        assert "s3" in candidates  # unsuccessful non-probe → step_repair
        assert "s4" not in candidates  # timeout → transient

    def test_contract_bound_step_excluded(self):
        sched = PlanningExecutorScheduler.__new__(PlanningExecutorScheduler)
        step = DagStep(id="s1", tool="browser", input={"url": "https://x"})
        r = StepResult(step_id="s1", exec_state=ExecState.FAILED, error="browser unavailable")
        plan = DagPlan(intent="t", steps=[step])
        contract = DeliveryContract(
            contract_id="c1", tool="browser", input={"url": "https://x"}, operation="navigate"
        )
        results = {"s1": r}
        assert sched._local_repair_candidates(plan, results, contracts=[contract]) == []
        assert sched._local_repair_candidates(plan, results, contracts=[]) == [step]

    def test_skipped_and_normal_steps_not_candidates(self):
        sched = PlanningExecutorScheduler.__new__(PlanningExecutorScheduler)
        step_done, r_done = _failing_result("s1", "http", "browser unavailable", state=ExecState.COMPLETED)
        step_skip, r_skip = _failing_result("s2", "http", "dep s1 not normal", state=ExecState.SKIPPED)
        plan = DagPlan(intent="t", steps=[step_done, step_skip])
        results = {"s1": r_done, "s2": r_skip}
        assert sched._local_repair_candidates(plan, results, contracts=[]) == []


# ── scheduler local-repair funnel (default-off + on) ──────────


class TestLocalRepairFunnel:
    async def test_disabled_by_default_writes_no_repair_events(self):
        store = EventStore(":memory:")
        await store.initialize()
        try:
            sched = await _make_scheduler(store, tool_defs=[_tool_def("http_request", ro_ops=["GET"])])
            sched.config = SchedulerConfig()  # default: local_repair_disabled
            # Funnel guards on _local_repair_budget() → None when disabled, so the
            # repair method is never reached and no STEP_LOCAL_REPAIR_* is written.
            assert sched._local_repair_budget() is None
            events = await store.get_events("run-x")
            assert not any(
                e.event_type in (EventType.STEP_LOCAL_REPAIR_STARTED, EventType.STEP_LOCAL_REPAIR_COMPLETED) for e in events
            )
        finally:
            await store.close()

    async def test_enabled_accepts_read_only_proposal_and_patches_plan(self):
        store = EventStore(":memory:")
        await store.initialize()
        try:
            http_def = _tool_def("http_request", ro_ops=["GET"])
            sched = await _make_scheduler(store, tool_defs=[http_def])
            sched.config = SchedulerConfig(
                local_repair_enabled=True,
                local_repair_allowed_tools=("http_request",),
                local_repair_max_rounds=2,
            )
            step, r = _failing_result("s1", "browser", "browser unavailable: asyncio not supported")

            async def _propose(*args, **kwargs):
                return LocalRepairProposal(step_id="s1", tool_name="http_request", input={"method": "GET", "url": "https://x"})

            sched.planner.propose_local_repair = _propose
            budget = RecoveryBudget(max_repair_rounds=2, allowed_tools=frozenset({"http_request"}))
            patched = await sched._attempt_step_local_repair(
                "run-x", DagPlan(intent="t", steps=[step]), "plan_x", {"s1": r}, {}, budget, []
            )
            assert patched is not None
            assert patched.steps[0].tool == "http_request"
            events = await store.get_events("run-x")
            types = [e.event_type for e in events]
            assert EventType.STEP_LOCAL_REPAIR_STARTED in types
            completed = next(e for e in events if e.event_type == EventType.STEP_LOCAL_REPAIR_COMPLETED)
            assert completed.payload["outcome"] == "accepted"
        finally:
            await store.close()

    async def test_rejects_mutating_or_non_whitelist_proposal(self):
        store = EventStore(":memory:")
        await store.initialize()
        try:
            # http_request with a real mutating POST (external side effect) so the
            # trusted map does NOT mark it fully-read-only → POST proposal rejected.
            http_def = ToolDefinition(
                name="http_request",
                description="http",
                input_schema={"type": "object", "properties": {"method": {"type": "string"}}},
                side_effects=[],
                operations=[
                    {
                        "operation": "GET",
                        "input_schema": {},
                        "side_effects": [],
                        "required_input": [],
                    },
                    {
                        "operation": "POST",
                        "input_schema": {},
                        "side_effects": [SideEffect.EXTERNAL],
                        "required_input": [],
                    },
                ],
            )
            sched = await _make_scheduler(store, tool_defs=[http_def])
            sched.config = SchedulerConfig(
                local_repair_enabled=True,
                local_repair_allowed_tools=("http_request",),
                local_repair_max_rounds=1,
            )
            step, r = _failing_result("s1", "browser", "browser unavailable")

            async def _propose_mutating(*args, **kwargs):
                return LocalRepairProposal(step_id="s1", tool_name="http_request", input={"method": "POST", "url": "https://x"})

            sched.planner.propose_local_repair = _propose_mutating
            budget = RecoveryBudget(max_repair_rounds=1, allowed_tools=frozenset({"http_request"}))
            patched = await sched._attempt_step_local_repair(
                "run-x", DagPlan(intent="t", steps=[step]), "plan_x", {"s1": r}, {}, budget, []
            )
            assert patched is None  # mutating POST rejected → no plan patch → global revise fallback
            events = await store.get_events("run-x")
            completed = next(e for e in events if e.event_type == EventType.STEP_LOCAL_REPAIR_COMPLETED)
            assert completed.payload["outcome"] == "rejected"
            assert "mutating" in completed.payload["reason"]
        finally:
            await store.close()

    async def test_no_repair_proposal_falls_through(self):
        store = EventStore(":memory:")
        await store.initialize()
        try:
            sched = await _make_scheduler(store, tool_defs=[_tool_def("http_request", ro_ops=["GET"])])
            sched.config = SchedulerConfig(
                local_repair_enabled=True,
                local_repair_allowed_tools=("http_request",),
                local_repair_max_rounds=2,
            )
            step, r = _failing_result("s1", "browser", "browser unavailable")

            async def _propose_none(*args, **kwargs):
                return None

            sched.planner.propose_local_repair = _propose_none
            budget = RecoveryBudget(max_repair_rounds=2, allowed_tools=frozenset({"http_request"}))
            patched = await sched._attempt_step_local_repair(
                "run-x", DagPlan(intent="t", steps=[step]), "plan_x", {"s1": r}, {}, budget, []
            )
            assert patched is None
        finally:
            await store.close()

    async def test_e2e_local_repair_avoids_global_revise(self):
        """Full scheduler run: a step fails non-transiently; local repair replaces
        its action; the DAG completes WITHOUT a global PlanRevised. This reproduces
        the e05087b6 pattern (step 1 browser-ish read of a missing file) bounded at
        the step level instead of escalating to a full plan rewrite."""
        import json
        from pathlib import Path

        from harness.core.llm_client import MockLLMClient
        from harness.execution.local import LocalDirectoryBackend
        from harness.tools.file_op import FileOpTool

        root = Path(__file__).resolve().parent.parent
        store = EventStore(":memory:")
        await store.initialize()
        try:
            executor = ToolExecutor(store)
            backend = LocalDirectoryBackend(str(root))
            reg = ToolRegistry()
            reg.register_tool(FileOpTool())
            defs, fns = reg.list_tool_defs(), reg.list_tool_fns()

            plan1 = json.dumps(
                {
                    "intent": "t",
                    "steps": [
                        {"id": "s1", "tool": "file_op", "input": {"operation": "read", "path": "nonexistent_file.xyz"}},
                    ],
                }
            )
            planner = Planner(MockLLMClient(responses=["yes", plan1, "answer"]), reg, store, max_plan_retries=2)
            dag = DagExecutor(executor, store, reg, backend=backend)
            sched = PlanningExecutorScheduler(
                store,
                executor,
                planner,
                dag,
                defs,
                fns,
                config=SchedulerConfig(
                    max_iterations=10,
                    local_repair_enabled=True,
                    local_repair_max_rounds=2,
                    local_repair_allowed_tools=("file_op",),
                ),
            )

            async def _propose_fix(*args, **kwargs):
                return LocalRepairProposal(
                    step_id="s1",
                    tool_name="file_op",
                    input={"operation": "read", "path": "README.md"},
                )

            planner.propose_local_repair = _propose_fix
            state = await sched.run("local_repair_e2e", "复现")
            events = await store.get_events("local_repair_e2e")
            types = [e.event_type for e in events]
            assert state.status.value == "completed", f"state={state.status} last_error={state.last_error}"
            assert EventType.STEP_LOCAL_REPAIR_STARTED in types
            assert EventType.STEP_LOCAL_REPAIR_COMPLETED in types
            # The step failure was absorbed by local repair — no global revision.
            assert EventType.PLAN_REVISED not in types
        finally:
            await store.close()


# ── event/model sync (AGENTS.md §6.3) ─────────────────────────


class TestEventSync:
    def test_repair_events_in_payload_map_and_fold_safe(self):
        from harness.core.fold import fold_events
        from harness.models.events import Event, PAYLOAD_MODEL_MAP

        assert EventType.STEP_LOCAL_REPAIR_STARTED in PAYLOAD_MODEL_MAP
        assert EventType.STEP_LOCAL_REPAIR_COMPLETED in PAYLOAD_MODEL_MAP
        events = [
            Event(run_id="r1", seq=1, event_type=EventType.RUN_STARTED, payload={"intent": "x"}, created_at=1.0),
            Event(
                run_id="r1",
                seq=2,
                event_type=EventType.STEP_LOCAL_REPAIR_STARTED,
                payload={"step_id": "s1", "plan_id": "p1", "repair_round": 1, "budget_remaining": 1, "step_tool": "browser"},
                created_at=2.0,
            ),
            Event(
                run_id="r1",
                seq=3,
                event_type=EventType.STEP_LOCAL_REPAIR_COMPLETED,
                payload={"step_id": "s1", "plan_id": "p1", "repair_round": 1, "outcome": "accepted", "proposed_tool": "http_request"},
                created_at=3.0,
            ),
        ]
        state = fold_events(events)
        assert state.status.value == "running"
