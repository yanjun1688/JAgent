"""v2.2 (Phase E) tests — probe 声明 (D4/D10) + 退化修订守卫 (U1 收敛闭环).

Covers:
  * _parse_plan 解析 probe 字段
  * PlanGuardrail 拒绝 probe + 有副作用工具（D10）
  * probe 步骤 UNSUCCESSFUL 算 normal（B 阶段已有，E 阶段补 LLM 声明路径）
  * 退化修订守卫：同一 step 反复非 normal 不收敛 → 熔断（U1）
"""

from __future__ import annotations

import pytest

from harness.core.dag_types import ExecState, StepResult
from harness.core.planner import PlanGuardrail, Planner
from harness.models.plan import DagPlan, DagStep
from harness.tools.registry import ToolRegistry


# ── 1. _parse_plan 解析 probe（D4）──────────────────────────────────


def _make_planner():
    return Planner(llm_client=None, registry=None, store=None)


def test_parse_plan_extracts_probe_flag():
    resp = (
        '{"intent":"t","steps":['
        '{"id":"s1","tool":"http_request","input":{"url":"http://a"},"probe":true},'
        '{"id":"s2","tool":"http_request","input":{"url":"http://b"}}]}'
    )
    plan, err = _make_planner()._parse_plan(resp)
    assert err == ""
    assert plan is not None
    assert plan.steps[0].probe is True
    assert plan.steps[1].probe is False


def test_parse_plan_probe_defaults_false():
    resp = '{"intent":"t","steps":[{"id":"s1","tool":"http_request","input":{"url":"http://a"}}]}'
    plan, err = _make_planner()._parse_plan(resp)
    assert err == ""
    assert plan.steps[0].probe is False


# ── 2. PlanGuardrail probe 信任校验（D10）────────────────────────────


def _registry_with_tool(side_effects) -> ToolRegistry:
    from harness.models.tools import ToolDefinition

    registry = ToolRegistry()
    registry._register(
        ToolDefinition(
            name="query_tool",
            description="read-only query",
            input_schema={},
            output_schema={},
            idempotency_key_fields=[],
            side_effects=side_effects,
            timeout_ms=30000,
        ),
        fn=lambda input: {"found": False},
    )
    return registry


def test_probe_allowed_for_side_effect_free_tool():
    registry = _registry_with_tool([])
    guardrail = PlanGuardrail(registry)
    plan = DagPlan(
        intent="probe-ok",
        steps=[DagStep(id="s1", tool="query_tool", input={}, probe=True)],
    )
    assert guardrail.validate(plan) == []


def test_probe_rejected_for_mutating_tool():
    from harness.models.tools import SideEffect

    registry = _registry_with_tool([SideEffect.WRITE])
    guardrail = PlanGuardrail(registry)
    plan = DagPlan(
        intent="probe-bad",
        steps=[DagStep(id="s1", tool="query_tool", input={}, probe=True)],
    )
    errors = guardrail.validate(plan)
    assert len(errors) == 1
    assert "probe" in errors[0]
    assert "side_effect" in errors[0] or "side_effects" in errors[0]


def test_probe_mutating_tool_rejected_on_revise_path():
    """D10: probe 校验在 revise 路径同样生效（available/completed ids 不影响）。"""
    from harness.models.tools import SideEffect

    registry = _registry_with_tool([SideEffect.DELETE])
    guardrail = PlanGuardrail(registry)
    plan = DagPlan(
        intent="probe-revise",
        steps=[DagStep(id="s2", tool="query_tool", input={}, probe=True, depends_on=["s1"])],
    )
    errors = guardrail.validate(plan, completed_step_ids={"s1"})
    assert len(errors) == 1
    assert "probe" in errors[0]


# ── 3. probe 步骤 UNSUCCESSFUL 算 normal（LLM 声明路径 e2e）───────────


def test_probe_declared_step_normal_when_unsuccessful():
    """D3+D4: LLM 声明 probe 的步骤，UNSUCCESSFUL 算正常。"""
    sr = StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL, probe=True)
    assert sr.step_normal is True
    assert sr.output_available is True
    assert sr.should_not_rerun is False  # 仍可重跑（工具未拿到东西，但语义上"没有"=正确）


# ── 4. 退化修订守卫（U1 收敛闭环，签名比对版）───────────────────────


def test_degenerate_guard_detects_repeated_step():
    """退化守卫（E 签名比对）：修订步骤与已 FAILED/UNSUCCESSFUL(非probe) 步骤
    (tool, 规范化 input) 相同，且依赖闭包无新步骤 → 判定退化。"""
    from harness.core.scheduler.plan import PlanningExecutorScheduler

    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
        ],
    )
    results = {
        "s1": StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL),
    }
    # 修订返回同一动作（同 tool+input）→ 退化
    revised = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
        ],
    )
    deg = PlanningExecutorScheduler._find_degenerate_revised_steps(plan, results, revised)
    assert deg == ["s1"]

    # 修订改变输入 → 新动作，不退化
    revised2 = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://b"}),
        ],
    )
    assert PlanningExecutorScheduler._find_degenerate_revised_steps(plan, results, revised2) == []

    # 修订新增上游步骤（可能改变输出）→ 不退化
    revised3 = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s0", tool="http_request", input={"url": "http://src"}),
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}, depends_on=["s0"]),
        ],
    )
    assert PlanningExecutorScheduler._find_degenerate_revised_steps(plan, results, revised3) == []


def test_degenerate_guard_ignores_probe_and_completed():
    """probe 步骤的 UNSUCCESSFUL 是正常结果；COMPLETED 非失败 → 两者不触发守卫。"""
    from harness.core.scheduler.plan import PlanningExecutorScheduler

    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}, probe=True),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}),
        ],
    )
    results = {
        "s1": StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL, probe=True),  # 探测否定 = normal
        "s2": StepResult(step_id="s2", exec_state=ExecState.COMPLETED),
    }
    revised = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}, probe=True),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}),
        ],
    )
    assert PlanningExecutorScheduler._find_degenerate_revised_steps(plan, results, revised) == []


def test_degenerate_guard_requires_same_tool():
    """仅同 tool+input 才算退化；换工具不算。"""
    from harness.core.scheduler.plan import PlanningExecutorScheduler

    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
        ],
    )
    results = {
        "s1": StepResult(step_id="s1", exec_state=ExecState.FAILED, error="timeout"),
    }
    revised = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="read_file", input={"path": "/tmp/a"}),
        ],
    )
    assert PlanningExecutorScheduler._find_degenerate_revised_steps(plan, results, revised) == []


@pytest.mark.asyncio
async def test_degenerate_self_heal_fails_run():
    """e2e: LLM 反复返回同一 UNSUCCESSFUL 步骤（同输入不收敛）→ 签名比对守卫
    拒绝退化修订（round 2 收敛），run FAILED with 'Degenerate self-heal'。"""
    from unittest.mock import AsyncMock

    from harness.core.dag_executor import DagExecutor
    from harness.core.llm_client import MockLLMClient
    from harness.core.planner import Planner
    from harness.core.scheduler.base import SchedulerConfig
    from harness.core.scheduler.plan import PlanningExecutorScheduler
    from harness.storage.event_store import EventStore
    from harness.tools.executor import ToolExecutor
    from harness.tools.http_request import HTTP_REQUEST_DEF
    from harness.tools.registry import ToolRegistry

    store = EventStore(":memory:")
    await store.initialize()
    try:
        executor = ToolExecutor(store)
        registry = ToolRegistry()
        registry._register(HTTP_REQUEST_DEF, lambda i: {"status_code": 429, "headers": {}, "body": "", "elapsed_ms": 10})
        plan1 = '{"intent":"t","steps":[{"id":"s1","tool":"http_request","input":{"url":"http://a"}}]}'
        planner = Planner(
            MockLLMClient(responses=["yes", plan1, plan1, plan1, plan1, plan1, "answer"]),
            registry,
            store,
            max_plan_retries=1,
        )
        dag = DagExecutor(executor, store, registry)
        sched = PlanningExecutorScheduler(
            store,
            executor,
            planner,
            dag,
            [],
            {},
            config=SchedulerConfig(max_consecutive_failures=3, max_iterations=20, max_revise_retries=2),
        )

        # LLM 每次 revise 都返回同一失败步骤（永不修复）→ 触发签名比对守卫
        async def fake_revise(*args, **kwargs):
            return DagPlan(
                intent="t",
                steps=[
                    DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
                ],
            )

        planner.revise = AsyncMock(side_effect=fake_revise)

        state = await sched.run("run-degenerate", "t")
        assert state.status.value == "failed"
        assert "Degenerate self-heal" in (state.last_error or ""), (
            f"expected degenerate guard message, got: {state.last_error}"
        )
    finally:
        await store.close()
