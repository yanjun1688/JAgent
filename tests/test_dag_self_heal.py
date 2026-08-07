"""Integration tests for DAG self-heal — completed steps must NOT re-execute.

Regression tests for a bug where, after a layer failure triggered revise(),
completed steps were re-scheduled when the revised plan reused the same step
ids. Fixed by:
  * _execute_plan: completed_ids no longer filters out in-plan steps
  * DagPlan.topological_sort: skips completed in-plan steps in the Kahn queue
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.llm_client import MockLLMClient
from harness.core.planner import Planner
from harness.core.scheduler.base import SchedulerConfig
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.file_op import FILE_OP_DEF, file_op_fn, reset_sandbox_root, set_sandbox_root
from harness.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _sandbox_isolation():
    """Isolate the module-global sandbox root.

    The self-heal path sets a sandbox root; restore the default afterwards so
    other tests (which rely on cwd-based resolution) are not polluted.
    """
    yield
    reset_sandbox_root()


def _build_engine():
    store = EventStore(":memory:")
    return store


async def _count_step_starts(store: EventStore, run_id: str) -> Counter:
    events = await store.get_events(run_id)
    counts: Counter = Counter()
    for e in events:
        if e.event_type.value == "DagStepStarted":
            counts[e.payload["step_id"]] += 1
    return counts


@pytest.mark.asyncio
async def test_self_heal_does_not_re_execute_completed_step():
    """s2 失败触发 revise 后，已完成 s1 不得再次执行。

    修复前：DagStepStarted 显示 s1 执行 2 次（原执行 + self-heal 重跑）。
    修复后：每个 step 仅执行 1 次。
    """
    store = EventStore(":memory:")
    await store.initialize()
    try:
        set_sandbox_root(str(ROOT))
        ex = ToolExecutor(store)
        reg = ToolRegistry()
        reg.register(FILE_OP_DEF, file_op_fn)
        defs, fns = reg.list_tool_defs(), reg.list_tool_fns()

        plan1 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s1", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}},
                {"id": "s2", "tool": "file_op", "input": {"operation": "read", "path": "nonexistent_file.xyz"}},
            ],
        })
        plan2 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s1", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}},
                {"id": "s3", "tool": "file_op", "input": {"operation": "read", "path": "pyproject.toml"}},
            ],
        })
        planner = Planner(
            MockLLMClient(responses=["yes", plan1, plan2, "answer"]),
            reg, store, max_plan_retries=2,
        )
        dag = DagExecutor(ex, store, reg)
        sched = PlanningExecutorScheduler(
            store, ex, planner, dag, defs, fns,
            config=SchedulerConfig(max_iterations=10),
        )
        state = await sched.run("self_heal_test", "复现")
        counts = await _count_step_starts(store, "self_heal_test")
        assert state.status.value == "completed"
        assert counts["s1"] == 1, f"s1 re-executed: {dict(counts)}"
        assert counts["s2"] == 1
        assert counts["s3"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_self_heal_reuses_ids_skips_in_plan_completed():
    """end-to-end: revised plan 复用 s1 id，s1 完成态应从拓扑层移除。"""
    from harness.models.plan import DagPlan, DagStep

    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s1", tool="file_op", input={}),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids={"s1"})
    assert layers == [["s2"]]


@pytest.mark.asyncio
async def test_soft_error_self_heal_reruns_only_failed_step():
    """场景3: 5步中第3步 soft_error，revise 标 not_achieved 后仅重跑 s3。

    修复前：s3 被 should_not_rerun 标记为完成 → 第二轮 topological_sort
    报 Cycle detected 或 layers=0 → 修正步骤永不执行。
    """
    store = EventStore(":memory:")
    await store.initialize()
    try:
        set_sandbox_root(str(ROOT))
        ex = ToolExecutor(store)
        reg = ToolRegistry()
        reg.register(FILE_OP_DEF, file_op_fn)
        defs, fns = reg.list_tool_defs(), reg.list_tool_fns()

        plan1 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s1", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}},
                {"id": "s2", "tool": "file_op", "input": {"operation": "read", "path": "pyproject.toml"}, "depends_on": ["s1"]},
                {"id": "s3", "tool": "file_op", "input": {"operation": "read", "path": "nonexistent_file.xyz"}, "depends_on": ["s2"]},
                {"id": "s4", "tool": "file_op", "input": {"operation": "read", "path": "AGENTS.md"}, "depends_on": ["s3"]},
                {"id": "s5", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}, "depends_on": ["s4"]},
            ],
        })
        plan2 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s3", "tool": "file_op", "input": {"operation": "read", "path": "AGENTS.md"}},
                {"id": "s4", "tool": "file_op", "input": {"operation": "read", "path": "AGENTS.md"}, "depends_on": ["s3"]},
                {"id": "s5", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}, "depends_on": ["s4"]},
            ],
            "step_tasks": {"s3": "not_achieved"},
        })
        planner = Planner(
            MockLLMClient(responses=["yes", plan1, plan2, "answer"]),
            reg, store, max_plan_retries=2,
        )
        dag = DagExecutor(ex, store, reg)
        sched = PlanningExecutorScheduler(
            store, ex, planner, dag, defs, fns,
            config=SchedulerConfig(max_iterations=10),
        )
        state = await sched.run("soft_error_heal", "复现")
        counts = await _count_step_starts(store, "soft_error_heal")
        assert state.status.value == "completed"
        assert counts["s1"] == 1, f"s1 re-executed: {dict(counts)}"
        assert counts["s2"] == 1, f"s2 re-executed: {dict(counts)}"
        assert counts["s3"] == 2, f"s3 should rerun exactly once: {dict(counts)}"
        assert counts["s4"] == 1, f"s4 re-executed: {dict(counts)}"
        assert counts["s5"] == 1, f"s5 re-executed: {dict(counts)}"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_topological_sort_no_false_cycle_with_completed():
    """completed 步骤的依赖不应引发误报 Cycle detected。"""
    from harness.models.plan import DagPlan, DagStep

    plan = DagPlan(
        intent="t",
        steps=[
            DagStep(id="s3", tool="file_op", input={}),
            DagStep(id="s4", tool="file_op", input={}, depends_on=["s3"]),
            DagStep(id="s5", tool="file_op", input={}, depends_on=["s4"]),
        ],
    )
    assert plan.topological_sort(completed_step_ids={"s5"}) == [["s3"], ["s4"]]
    assert plan.topological_sort(completed_step_ids={"s4", "s5"}) == [["s3"]]


def test_topological_sort_true_cycle_still_detected():
    """真正的环（无 completed 干扰）仍必须抛出 Cycle detected。"""
    from harness.models.plan import DagPlan, DagStep

    plan = DagPlan(
        intent="true-cycle",
        steps=[
            DagStep(id="s1", tool="file_op", input={}, depends_on=["s2"]),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
        ],
    )
    with pytest.raises(ValueError, match="Cycle detected"):
        plan.topological_sort()


def test_completed_step_never_enters_schedule_queue():
    """锁定 Bug 2 的核心不变量：completed 步骤必须真正从调度队列移除。

    背景（Bug 2）：topological_sort 的 completed_step_ids 参数名声称"跳过已
    完成步骤"，但旧实现只在依赖合法性校验里用到它，Kahn 队列初始化与出队
    时完全没过滤 completed → s1 照样入队执行 → 自愈后已完成步骤重复执行，
    token 白白消耗。修复后队列三处（初始入队/出队/解锁邻居）都过滤 completed。

    这个测试用「s1 在 plan 内且已完成」的真实 self-heal 场景断言：
      * completed 的 s1 不得出现在任何 layer 中
      * 依赖它的 s2 正常调度
      * visited 计数不重复 → 不误报 Cycle detected
    """
    from harness.models.plan import DagPlan, DagStep

    plan = DagPlan(
        intent="self-heal-reuse-id",
        steps=[
            DagStep(id="s1", tool="file_op", input={}),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
            DagStep(id="s3", tool="file_op", input={}, depends_on=["s2"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids={"s1"})
    assert layers == [["s2"], ["s3"]], f"completed s1 leaked into schedule: {layers}"
    assert all("s1" not in layer for layer in layers), "s1 must never be re-scheduled"


def test_completed_step_in_middle_layer_skipped():
    """completed 步骤处于中间层时，其上游/下游都能被正确调度。

    s2 已完成 → s2 不调度；s1 无依赖入队；s3 依赖 s2 但 s2 已完成
    不再计数 in_degree → s3 也归入第 0 层。两者可并行。
    """
    from harness.models.plan import DagPlan, DagStep

    plan = DagPlan(
        intent="mid-completed",
        steps=[
            DagStep(id="s1", tool="file_op", input={}),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
            DagStep(id="s3", tool="file_op", input={}, depends_on=["s2"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids={"s2"})
    assert layers == [["s1", "s3"]], f"mid completed step mishandled: {layers}"
    assert all("s2" not in layer for layer in layers), "s2 must never be re-scheduled"
