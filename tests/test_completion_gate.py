"""v2.2 (Phase D) tests — 完成判定机械化 (D5, U2 根治) + task_state 落事件 (D11) + 终态证据 (洞 5).

Covers:
  * _completion_gate: 最终计划所有步骤 step_normal 聚合，不信 LLM 空 steps
  * RUN_COMPLETED 携带 all_normal / unmet_step_ids 机械证据
  * PlanRevisedPayload 携带 step_tasks 审计便签（D11）
  * 假绿消灭：revise 空 steps 但存在 unmet → run FAILED
"""

from __future__ import annotations

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import ExecState, StepResult, TaskState
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType
from harness.models.plan import DagPlan, DagStep
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.http_request import HTTP_REQUEST_DEF
from harness.tools.registry import ToolRegistry

_OK_TOOL_FN = lambda i: {"status_code": 200, "headers": {}, "body": '{"ok":1}', "elapsed_ms": 10}  # noqa: E731
_UNSUCCESSFUL_TOOL_FN = lambda i: {"status_code": 429, "headers": {}, "body": "", "elapsed_ms": 10}  # noqa: E731


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


# ── 1. _completion_gate 纯函数（D5）────────────────────────────────


def test_completion_gate_all_normal():
    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request"),
            DagStep(id="s2", tool="http_request"),
        ],
    )
    results = {
        "s1": StepResult("s1", exec_state=ExecState.COMPLETED),
        "s2": StepResult("s2", exec_state=ExecState.IDEMPOTENT),
    }
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is True
    assert unmet == []


def test_completion_gate_unmet_on_unsuccessful():
    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request"),
            DagStep(id="s2", tool="http_request"),
        ],
    )
    results = {
        "s1": StepResult("s1", exec_state=ExecState.COMPLETED),
        "s2": StepResult("s2", exec_state=ExecState.UNSUCCESSFUL),
    }
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is False
    assert unmet == ["s2"]


def test_completion_gate_unmet_on_failed_and_skipped():
    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request"),
            DagStep(id="s2", tool="http_request"),
            DagStep(id="s3", tool="http_request"),
        ],
    )
    results = {
        "s1": StepResult("s1", exec_state=ExecState.FAILED),
        "s2": StepResult("s2", exec_state=ExecState.SKIPPED),
        "s3": StepResult("s3", exec_state=ExecState.COMPLETED),
    }
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is False
    assert sorted(unmet) == ["s1", "s2"]


def test_completion_gate_probe_unsuccessful_is_normal():
    """D4/D7: probe 步骤 UNSUCCESSFUL 算 normal → 完成门通过。"""
    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request", probe=True),
        ],
    )
    results = {"s1": StepResult("s1", exec_state=ExecState.UNSUCCESSFUL, probe=True)}
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is True
    assert unmet == []


def test_completion_gate_missing_step_is_unmet():
    """计划中的步骤无结果 → unmet（未执行完）。"""
    plan = DagPlan(intent="t", steps=[DagStep(id="s1", tool="http_request")])
    results = {}
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is False
    assert unmet == ["s1"]


def test_completion_gate_ignores_task_state():
    """约束 4: 完成门不读 task_state — 即使 LLM 标 achieved，UNSUCCESSFUL 仍 unmet。"""
    plan = DagPlan(intent="t", steps=[DagStep(id="s1", tool="http_request")])
    results = {"s1": StepResult("s1", exec_state=ExecState.UNSUCCESSFUL, task_state=TaskState.ACHIEVED)}
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is False
    assert unmet == ["s1"]


# ── 2. 假绿消灭 e2e：UNSUCCESSFUL 未被修复 → run FAILED（U2 根治）──


async def _make_sched(store, plan_responses, revise_response):
    from unittest.mock import AsyncMock

    from harness.core.llm_client import MockLLMClient
    from harness.core.planner import Planner
    from harness.core.scheduler.base import SchedulerConfig

    executor = ToolExecutor(store)
    registry = ToolRegistry()
    registry._register(HTTP_REQUEST_DEF, _UNSUCCESSFUL_TOOL_FN)
    planner = Planner(MockLLMClient(responses=plan_responses), registry, store, max_plan_retries=1)
    dag = DagExecutor(executor, store, registry)

    sched = PlanningExecutorScheduler(
        store,
        executor,
        planner,
        dag,
        [],
        {},
        config=SchedulerConfig(max_iterations=10),
    )
    sched.dag = dag
    # Override revise to return a fixed empty plan (LLM "says done" but steps unmet)
    planner.revise = AsyncMock(return_value=revise_response)
    return sched


@pytest.mark.asyncio
async def test_run_fails_when_revise_empty_but_step_unmet(store):
    """U2 根治：s1 永远 UNSUCCESSFUL，revise 返回空 → 完成门拦截 → FAILED。"""
    plan_resp = '{"intent":"t","steps":[{"id":"s1","tool":"http_request","input":{"url":"http://a"}}]}'
    empty = DagPlan(intent="t", steps=[], step_tasks={"s1": "achieved"})
    sched = await _make_sched(store, ["yes", plan_resp], empty)

    state = await sched.run("run-u2", "t")
    assert state.status.value == "failed"
    assert "Steps not achieved" in (state.last_error or "")

    events = await store.get_events("run-u2")
    assert not any(e.event_type == EventType.RUN_COMPLETED for e in events), "禁止假绿 RUN_COMPLETED"
    assert any(e.event_type == EventType.RUN_FAILED for e in events)


@pytest.mark.asyncio
async def test_run_completes_when_all_steps_normal(store):
    """全 normal → 完成门通过 → RUN_COMPLETED 携带 all_normal=True。"""
    from unittest.mock import AsyncMock

    from harness.core.llm_client import MockLLMClient
    from harness.core.planner import Planner
    from harness.core.scheduler.base import SchedulerConfig

    executor = ToolExecutor(store)
    registry = ToolRegistry()
    registry._register(HTTP_REQUEST_DEF, _OK_TOOL_FN)
    planner = Planner(
        MockLLMClient(
            responses=[
                "yes",
                '{"intent":"t","steps":[{"id":"s1","tool":"http_request","input":{"url":"http://a"}}]}',
                "answer",
            ]
        ),
        registry,
        store,
        max_plan_retries=1,
    )
    dag = DagExecutor(executor, store, registry)
    sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {}, config=SchedulerConfig(max_iterations=10))
    sched.dag = dag
    planner.generate_answer = AsyncMock(return_value="Done")

    state = await sched.run("run-ok", "t")
    assert state.status.value == "completed"

    events = await store.get_events("run-ok")
    completed = next(e for e in events if e.event_type == EventType.RUN_COMPLETED)
    assert completed.payload["all_normal"] is True
    assert completed.payload["unmet_step_ids"] == []


# ── 3. task_state 审计便签落事件（D11）──────────────────────────────


@pytest.mark.asyncio
async def test_plan_revised_carries_step_tasks_audit_note(store):
    """PlanRevisedPayload 携带 step_tasks（LLM 便签）供审计 + 未来差异展示。"""
    from unittest.mock import AsyncMock

    from harness.core.llm_client import MockLLMClient
    from harness.core.planner import Planner
    from harness.core.scheduler.base import SchedulerConfig

    executor = ToolExecutor(store)
    registry = ToolRegistry()
    registry._register(HTTP_REQUEST_DEF, _UNSUCCESSFUL_TOOL_FN)
    planner = Planner(
        MockLLMClient(
            responses=[
                "yes",
                '{"intent":"t","steps":[{"id":"s1","tool":"http_request","input":{"url":"http://a"}}]}',
                "answer",
            ]
        ),
        registry,
        store,
        max_plan_retries=1,
    )
    dag = DagExecutor(executor, store, registry)
    sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {}, config=SchedulerConfig(max_iterations=10))
    sched.dag = dag

    # LLM 自评: s1 achieved（系统机械判定: UNSUCCESSFUL → unmet）
    async def fake_revise(*args, **kwargs):
        return DagPlan(intent="t", steps=[], step_tasks={"s1": "achieved"})

    planner.revise = AsyncMock(side_effect=fake_revise)

    await sched.run("run-audit", "t")
    events = await store.get_events("run-audit")

    revised = [e for e in events if e.event_type == EventType.PLAN_REVISED]
    assert revised, "应写入 PLAN_REVISED"
    last = revised[-1]
    assert last.payload["step_tasks"].get("s1") == "achieved", "D11: task_state 便签应随 PLAN_REVISED 落事件"


# ── 4. fold 折叠：RUN_COMPLETED 证据反映到 RunState（洞 5）────────────


@pytest.mark.asyncio
async def test_fold_surfaces_completion_evidence(store):
    """fold_events 后 state.completion_evidence 携带 all_normal / unmet_step_ids。"""
    from harness.core.fold import fold_events
    from harness.models.events import RunCompletedPayload

    await store.append_event(
        "r",
        EventType.RUN_STARTED,
        {"intent": "t", "context_snapshot": {}},
    )
    await store.append_event(
        "r",
        EventType.RUN_COMPLETED,
        RunCompletedPayload(
            result_summary="done",
            all_normal=False,
            unmet_step_ids=["s2"],
        ).model_dump(),
    )
    events = await store.get_events("r")
    state = fold_events(events)

    assert state.status.value == "completed"
    assert state.completion_evidence == {
        "all_normal": False,
        "unmet_step_ids": ["s2"],
        "deliverable_met": None,
        "deliverable_status": "unverified",
        "deliverable_summary": [],
    }


@pytest.mark.asyncio
async def test_fold_completion_evidence_all_normal_default(store):
    """无 evidence 字段的 RUN_COMPLETED（默认全 normal）→ 折叠为 all_normal=True。"""
    from harness.core.fold import fold_events
    from harness.models.events import RunCompletedPayload

    await store.append_event(
        "r",
        EventType.RUN_STARTED,
        {"intent": "t", "context_snapshot": {}},
    )
    await store.append_event(
        "r",
        EventType.RUN_COMPLETED,
        RunCompletedPayload(result_summary="done").model_dump(),
    )
    state = fold_events(await store.get_events("r"))
    assert state.completion_evidence == {
        "all_normal": True,
        "unmet_step_ids": [],
        "deliverable_met": None,
        "deliverable_status": "unverified",
        "deliverable_summary": [],
    }


# ── 5. Q-02: declared_operations（LLM 自检声明）机械维度检查 ───────


def test_declared_op_step_satisfies_structural():
    """结构化子集匹配：step 满足 declared op = tool 相同 + input 键值子集。"""
    from harness.models.plan import DagStep, RequiredOperation

    step = DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt", "content": "x"})
    assert RequiredOperation.step_satisfies(step, RequiredOperation(tool="file_op", input={"operation": "write"}))
    assert RequiredOperation.step_satisfies(
        step, RequiredOperation(tool="file_op", input={"operation": "write", "path": "blackbox.txt"})
    )
    # 不同工具 / 不同 operation / 缺少键 → 不满足
    assert not RequiredOperation.step_satisfies(step, RequiredOperation(tool="file_op", input={"operation": "read"}))
    assert not RequiredOperation.step_satisfies(step, RequiredOperation(tool="http_request", input={}))
    assert not RequiredOperation.step_satisfies(
        step, RequiredOperation(tool="file_op", input={"operation": "write", "path": "other.txt"})
    )


def test_completion_gate_declared_op_unmet():
    """declared op 无匹配 step 达成 → unmet（禁止观察替代交付）。"""
    from harness.core.scheduler.plan import PlanningExecutorScheduler
    from harness.models.plan import DagPlan, DagStep, RequiredOperation

    plan = DagPlan(
        intent="t",
        steps=[DagStep(id="s1", tool="file_op", input={"operation": "list", "path": "."})],
        declared_operations=[RequiredOperation(tool="file_op", input={"operation": "write", "path": "blackbox.txt"})],
    )
    results = {"s1": StepResult("s1", exec_state=ExecState.COMPLETED)}
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is False
    assert any("declared_op" in u for u in unmet)


def test_completion_gate_declared_op_met():
    """declared op 有匹配 step 且达成 → 通过。"""
    from harness.models.plan import DagPlan, DagStep, RequiredOperation

    plan = DagPlan(
        intent="t",
        steps=[DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt", "content": "x"})],
        declared_operations=[RequiredOperation(tool="file_op", input={"operation": "write", "path": "blackbox.txt"})],
    )
    results = {"s1": StepResult("s1", exec_state=ExecState.COMPLETED)}
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is True
    assert unmet == []


def test_completion_gate_declared_op_unmet_on_unsuccessful():
    """declared op 匹配 step 但 UNSUCCESSFUL（非 probe）→ 仍 unmet。"""
    from harness.models.plan import DagPlan, DagStep, RequiredOperation

    plan = DagPlan(
        intent="t",
        steps=[DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt", "content": "x"})],
        declared_operations=[RequiredOperation(tool="file_op", input={"operation": "write", "path": "blackbox.txt"})],
    )
    results = {"s1": StepResult("s1", exec_state=ExecState.UNSUCCESSFUL)}
    verdict = PlanningExecutorScheduler._completion_gate(plan, results)
    all_normal, unmet = verdict.mechanical_complete, verdict.unmet_step_ids
    assert all_normal is False
    assert any("declared_op" in u for u in unmet)


@pytest.mark.asyncio
async def test_plan_guardrail_rejects_plan_missing_declared_op(store):
    """PlanGuardrail（受信）拒绝缺 declared op 匹配 step 的计划（结构自洽检查）→ Planner 重试。"""
    from harness.core.planner import PlanGuardrail
    from harness.models.plan import DagPlan, DagStep, RequiredOperation
    from harness.tools.file_op import FileOpTool
    from harness.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry._register(FileOpTool().to_definition(), lambda i: {})
    guardrail = PlanGuardrail(registry)

    plan = DagPlan(
        intent="t",
        steps=[DagStep(id="s1", tool="file_op", input={"operation": "list", "path": "."})],
        declared_operations=[RequiredOperation(tool="file_op", input={"operation": "write", "path": "blackbox.txt"})],
    )
    errors = guardrail.validate(plan)
    assert any("Declared operation" in e for e in errors)


@pytest.mark.asyncio
async def test_plan_guardrail_accepts_plan_with_declared_op(store):
    """PlanGuardrail 通过含 declared op 且自洽的计划。"""
    from harness.core.planner import PlanGuardrail
    from harness.models.plan import DagPlan, DagStep, RequiredOperation
    from harness.tools.file_op import FileOpTool
    from harness.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry._register(FileOpTool().to_definition(), lambda i: {})
    guardrail = PlanGuardrail(registry)

    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt", "content": "x"}),
            DagStep(id="s2", tool="file_op", input={"operation": "read", "path": "blackbox.txt"}, depends_on=["s1"]),
        ],
        declared_operations=[
            RequiredOperation(tool="file_op", input={"operation": "write", "path": "blackbox.txt"}),
            RequiredOperation(tool="file_op", input={"operation": "read", "path": "blackbox.txt"}),
        ],
    )
    errors = guardrail.validate(plan)
    assert errors == []
