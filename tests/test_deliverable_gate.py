"""S06 — 覆盖检查 + 完成门升级（deliverable_met 分离）测试。

覆盖 D-03（操作+路径级判定）、D-04（unverified 标记）、D-05（多交付物）、
C-02（绝不宣称交付达成）、C-06（验证器只对照契约）。双维判定：
mechanical_complete（step_normal 聚合 + declared_operations，Q-02 机械维度）与
deliverable_met（DeliveryContract 逐条判定）正交分层。
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import ExecState, StepResult
from harness.core.fold import RunStatus, fold_events
from harness.core.scheduler.plan import PlanningExecutorScheduler, verify_deliverables
from harness.models.events import EventType, RunCompletedPayload
from harness.models.intent import DeliveryContract, DeliverySource
from harness.models.plan import DagPlan, DagStep
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry


def _contract(tool: str, **input_kwargs) -> DeliveryContract:
    return DeliveryContract(tool=tool, input=input_kwargs, source=DeliverySource.CALLER)


def _plan(*steps: DagStep) -> DagPlan:
    return DagPlan(intent="t", steps=list(steps))


def _results(mapping: dict[str, ExecState]) -> dict[str, StepResult]:
    return {sid: StepResult(sid, exec_state=state) for sid, state in mapping.items()}


# ── DeliverableVerification（C-06 纯函数）────────────────────────


def test_contract_met_when_matching_step_normal():
    contracts = [
        _contract("file_op", operation="write", path="blackbox.txt"),
        _contract("file_op", operation="read", path="blackbox.txt"),
    ]
    plan = _plan(
        DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt"}),
        DagStep(id="s2", tool="file_op", input={"operation": "read", "path": "blackbox.txt"}, depends_on=["s1"]),
    )
    results = _results({"s1": ExecState.COMPLETED, "s2": ExecState.COMPLETED})
    verdicts = verify_deliverables(contracts, plan, results)
    assert [v.status for v in verdicts] == ["met", "met"]
    assert all(v.matched_step_ids for v in verdicts)


def test_missing_write_contract_unmet():
    contracts = [_contract("file_op", operation="write", path="blackbox.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "read", "path": "blackbox.txt"}))
    results = _results({"s1": ExecState.COMPLETED})
    verdicts = verify_deliverables(contracts, plan, results)
    assert verdicts[0].status == "unmet"
    assert verdicts[0].matched_step_ids == []


def test_path_change_makes_contract_unmet():
    contracts = [_contract("file_op", operation="write", path="blackbox.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "other.txt"}))
    results = _results({"s1": ExecState.COMPLETED})
    verdicts = verify_deliverables(contracts, plan, results)
    assert verdicts[0].status == "unmet"


def test_content_is_stored_but_not_a_delivery_match_key():
    contracts = [_contract("file_op", operation="write", path="x.txt", content="hello")]
    plan = _plan(
        DagStep(
            id="s1",
            tool="file_op",
            input={"operation": "write", "path": "x.txt", "content": "wrong"},
        )
    )
    results = _results({"s1": ExecState.COMPLETED})
    verdicts = verify_deliverables(contracts, plan, results)
    assert verdicts[0].status == "met"


def test_read_replaced_by_list_is_unmet():
    """观察替代交付（read→list）不得满足 read 契约。"""
    contracts = [_contract("file_op", operation="read", path="blackbox.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "list", "path": "."}))
    results = _results({"s1": ExecState.COMPLETED})
    verdicts = verify_deliverables(contracts, plan, results)
    assert verdicts[0].status == "unmet"


def test_list_contract_with_workspace_directory_aliases_met():
    """Bug B 回归（P1-13 13.4）：LLM 抽取的 `list path="workspace directory"`
    与 Planner 实际执行的 `list path="."` 语义等价，必须判定 met（不得误报
    Deliverable not met）。"""
    contracts = [_contract("file_op", operation="list", path="workspace directory")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "list", "path": "."}))
    results = _results({"s1": ExecState.COMPLETED})
    verdicts = verify_deliverables(contracts, plan, results)
    assert verdicts[0].status == "met"


def test_list_contract_workspace_root_alias_variants_met():
    """同一 workspace 根路径的多种人类可读写法都应归一化到 '.'。"""
    aliases = ["workspace", "workspace directory", "./", ".", "current directory", "the workspace"]
    for alias in aliases:
        contracts = [_contract("file_op", operation="list", path=alias)]
        plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "list", "path": "."}))
        results = _results({"s1": ExecState.COMPLETED})
        verdicts = verify_deliverables(contracts, plan, results)
        assert verdicts[0].status == "met", f"alias {alias!r} not normalized"


def test_contract_not_met_when_step_unsuccessful():
    contracts = [_contract("file_op", operation="write", path="blackbox.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt"}))
    results = _results({"s1": ExecState.UNSUCCESSFUL})
    verdicts = verify_deliverables(contracts, plan, results)
    assert verdicts[0].status == "unmet"


# ── CompletionVerdict：双维判定 ──────────────────────────────────


def test_mechanical_and_deliverable_met():
    contracts = [_contract("file_op", operation="write", path="a.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "a.txt"}))
    results = _results({"s1": ExecState.COMPLETED})
    v = PlanningExecutorScheduler._completion_gate(plan, results, contracts=contracts)
    assert v.mechanical_complete is True
    assert v.deliverable_met is True
    assert v.deliverable_status == "met"


def test_deliverable_unmet_despite_mechanical_complete():
    """契约含 write，计划只有 read → 机械全 normal 但 deliverable_met=False。"""
    contracts = [_contract("file_op", operation="write", path="blackbox.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "read", "path": "blackbox.txt"}))
    results = _results({"s1": ExecState.COMPLETED})
    v = PlanningExecutorScheduler._completion_gate(plan, results, contracts=contracts)
    assert v.mechanical_complete is True
    assert v.deliverable_met is False
    assert v.deliverable_status == "failed"
    assert v.deliverables[0].status == "unmet"


def test_empty_contracts_yields_unverified():
    plan = _plan(DagStep(id="s1", tool="http_request", input={}))
    results = _results({"s1": ExecState.COMPLETED})
    v = PlanningExecutorScheduler._completion_gate(plan, results, contracts=[])
    assert v.mechanical_complete is True
    assert v.deliverable_met is False
    assert v.deliverable_status == "unverified"


def test_multiple_contracts_all_must_met():
    contracts = [
        _contract("file_op", operation="write", path="a.txt"),
        _contract("file_op", operation="read", path="a.txt"),
        _contract("file_op", operation="write", path="b.txt"),
    ]
    plan = _plan(
        DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "a.txt"}),
        DagStep(id="s2", tool="file_op", input={"operation": "read", "path": "a.txt"}, depends_on=["s1"]),
        DagStep(id="s3", tool="file_op", input={"operation": "write", "path": "other.txt"}),
    )
    results = _results({"s1": ExecState.COMPLETED, "s2": ExecState.COMPLETED, "s3": ExecState.COMPLETED})
    v = PlanningExecutorScheduler._completion_gate(plan, results, contracts=contracts)
    assert v.deliverable_met is False
    assert v.deliverable_status == "failed"
    assert [d.status for d in v.deliverables] == ["met", "met", "unmet"]


def test_task_state_never_influences_deliverable():
    contracts = [_contract("file_op", operation="write", path="a.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "a.txt"}))
    results = _results({"s1": ExecState.UNSUCCESSFUL})
    results["s1"].task_state = "achieved"  # type: ignore[attr-defined]
    v = PlanningExecutorScheduler._completion_gate(plan, results, contracts=contracts)
    assert v.deliverable_met is False


# ── RunCompletedPayload + fold（事件链）──────────────────────────


def _run_completed_payload(verdict) -> RunCompletedPayload:
    return RunCompletedPayload(
        result_summary="done",
        all_normal=verdict.mechanical_complete,
        unmet_step_ids=verdict.unmet_step_ids,
        deliverable_met=verdict.deliverable_met,
        deliverable_status=verdict.deliverable_status,
        deliverable_summary=[asdict(d) for d in verdict.deliverables],
    )


async def _fold_with_run_completed(store, payload):
    await store.append_event("r", EventType.RUN_STARTED, {"intent": "t"})
    await store.append_event("r", EventType.RUN_COMPLETED, payload.model_dump())
    return fold_events(await store.get_events("r"))


def test_run_completed_payload_roundtrip(store):
    from harness.models.events import RunStartedPayload
    from harness.models.intent import DeliveryContract

    contract = DeliveryContract(tool="file_op", input={"operation": "write", "path": "a.txt"})
    payload = RunStartedPayload(intent="t", intent_raw="t", contracts=[contract])
    dumped = payload.model_dump()
    restored = RunStartedPayload(**dumped)
    assert restored.contracts[0].tool == "file_op"


@pytest.mark.asyncio
async def test_fold_exposes_deliverable_status(store):
    contracts = [_contract("file_op", operation="write", path="a.txt")]
    plan = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "a.txt"}))
    results = _results({"s1": ExecState.COMPLETED})
    verdict = PlanningExecutorScheduler._completion_gate(plan, results, contracts=contracts)
    state = await _fold_with_run_completed(store, _run_completed_payload(verdict))
    assert state.completion_evidence["deliverable_met"] is True
    assert state.completion_evidence["deliverable_status"] == "met"


@pytest.mark.asyncio
async def test_fold_exposes_unverified_marking(store):
    plan = _plan(DagStep(id="s1", tool="http_request", input={}))
    results = _results({"s1": ExecState.COMPLETED})
    verdict = PlanningExecutorScheduler._completion_gate(plan, results, contracts=[])
    state = await _fold_with_run_completed(store, _run_completed_payload(verdict))
    assert state.completion_evidence["deliverable_met"] is False
    assert state.completion_evidence["deliverable_status"] == "unverified"


# ── 端到端（fake LLM）：Planner 丢 write → Run 不得 deliverable_met ──


@pytest.mark.asyncio
async def test_e2e_planner_dropping_write_never_claims_deliverable(store):
    """S06 验收 #3: 用户要求"创建+读取 blackbox.txt"，Planner 只给 read →
    write 契约 unmet → Run 不得 deliverable_met（绝不假绿）。"""
    from unittest.mock import AsyncMock

    from harness.core.llm_client import MockLLMClient
    from harness.core.planner import Planner
    from harness.core.scheduler.base import SchedulerConfig
    from harness.models.intent import DeliveryContract, DeliverySource
    from harness.tools.file_op import FileOpTool

    registry = ToolRegistry()
    file_op_def = FileOpTool().to_definition()
    registry._register(file_op_def, lambda x: {"success": True, "path": x.get("path", "")})
    planner = Planner(MockLLMClient(responses=[]), registry, store, max_plan_retries=1)
    executor = ToolExecutor(store)
    dag = DagExecutor(executor, store, registry)

    sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {}, config=SchedulerConfig(max_iterations=5))

    async def fake_plan(intent, state=None, feedback=None, conversation_context="", run_id=None):
        # Planner 弱化：只生成 read，丢掉 write
        return DagPlan(
            intent="read blackbox.txt",
            user_intent=intent,
            steps=[DagStep(id="s1", tool="file_op", input={"operation": "read", "path": "blackbox.txt"})],
            declared_operations=[],
        )

    planner.plan = fake_plan
    # revise 返回空 steps（LLM 认为完成）
    planner.revise = AsyncMock(return_value=DagPlan(intent="done", steps=[]))

    contracts = [
        DeliveryContract(
            tool="file_op", input={"operation": "write", "path": "blackbox.txt"}, source=DeliverySource.CALLER
        ),
        DeliveryContract(tool="file_op", input={"operation": "read", "path": "blackbox.txt"}, source=DeliverySource.CALLER),
    ]
    await store.append_event(
        "r1",
        EventType.RUN_STARTED,
        {
            "intent": "create and read blackbox.txt",
            "intent_raw": "create and read blackbox.txt",
            "contracts": [c.model_dump() for c in contracts],
        },
    )
    # fake read 工具会失败（文件不存在）→ UNSUCCESSFUL
    state = await sched.run("r1", "create and read blackbox.txt")
    assert state.status == RunStatus.FAILED
    evidence = state.completion_evidence or {}
    assert evidence.get("deliverable_met") is not True
