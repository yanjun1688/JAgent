"""缺口 4 · 工具契约快照接线（STATUS 缺口表 #4）。

``plan_hash`` 只证明「参数没变」，证明不了「工具还是那个工具」：冻结之后
registry 里的 ``fs.write`` 被换掉（插件重载/测试注入/热更新），同一个计划
会以不同语义执行，重放也不再可信。

接线后的契约（实现方案在 ``ext/contract_snapshot.py`` 的 docstring 里）：
- 冻结：``PLAN_FROZEN`` 载荷带 ``tool_snapshot``（对全部 ToolSpec 的稳定哈希，
  ``observations`` 参与哈希——观测面变了旧断言会悄悄退化）；
- 派发：executor 每次 ``registry.call`` 之前复核；不符 →
  ``ToolResult(ok=False, error_code="tool_contract_changed")``，工具一次都不跑；
  属策略拒绝（非环境错误），不升级人工、直接失败；
- 重放：快照随事件流持久，恢复时从 ``PLAN_FROZEN`` 读，不读进程内缓存。
"""

from __future__ import annotations

import dataclasses

from jagent.contracts import EventType, Plan, Step, ToolResult, Verify, plan_to_dict
from jagent.harness.contract_snapshot import SNAPSHOT_PREFIX, snapshot

from .conftest import Rig
from .test_runner_cancel import _probe_spec


def _probe_plan() -> Plan:
    return Plan(steps=(Step(id="probe", tool="probe", verify=Verify("True", needs=())),))


def _ok(args, ws, t) -> ToolResult:
    return ToolResult(ok=True)


def test_plan_frozen_records_tool_snapshot(rig: Rig) -> None:
    """Given runner 冻结计划，When start_and_run，
    Then PLAN_FROZEN 载荷带 tool-contract-v1 前缀的快照，
    重放（reload）后投影里仍是同一个值。"""
    rig.registry.add(_probe_spec("probe"), _ok)
    runner = rig.runner()
    outcome = runner.start_and_run(_probe_plan())

    frozen = rig.of_type(EventType.PLAN_FROZEN)[-1].payload
    assert frozen["tool_snapshot"].startswith(SNAPSHOT_PREFIX)
    assert rig.world.runs[outcome.run_id].tool_snapshot == frozen["tool_snapshot"]

    rig.reload()
    assert rig.world.runs[outcome.run_id].tool_snapshot == frozen["tool_snapshot"], (
        "快照随事件流持久，重放不读进程内缓存"
    )


def test_contract_change_rejects_dispatch_and_never_runs_the_tool(rig: Rig) -> None:
    """Given 冻结后的 registry 被换掉同名工具（timeout_s 变了 = 契约变了），
    When drive，Then 派发被拒（tool_contract_changed）、工具函数零调用、
    run 诚实失败。"""
    calls: list[str] = []
    rig.registry.add(_probe_spec("probe"), lambda args, ws, t: calls.append("ran") or _ok(args, ws, t))

    run_id = "run-contract"
    rig.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=run_id,
    )
    rig.writer.emit(
        EventType.PLAN_FROZEN,
        {
            "plan": plan_to_dict(_probe_plan()),
            "plan_hash": _probe_plan().hash(),
            "version": 1,
            "tool_snapshot": snapshot(rig.registry.specs()),
        },
        run_id=run_id,
    )

    # 冻结之后换工具：同名的 spec，timeout_s 不同 = 一次调用的含义变了。
    swapped = dataclasses.replace(_probe_spec("probe"), timeout_s=99.0)
    rig.registry.add(swapped, lambda args, ws, t: calls.append("ran") or _ok(args, ws, t))

    outcome = rig.runner().drive(run_id)

    assert outcome.status == "failed"
    assert calls == [], "契约变了还执行工具 = 冻结白冻"
    finished = [e.payload for e in rig.events() if e.type is EventType.STEP_FINISHED]
    assert finished and finished[-1]["error_code"] == "tool_contract_changed"
    classified = [e.payload for e in rig.events() if e.type is EventType.FAILURE_CLASSIFIED]
    assert classified and classified[-1]["error_code"] == "tool_contract_changed"


def test_unchanged_contract_runs_normally(rig: Rig) -> None:
    """Given 冻结后工具没被换，When drive，Then 正常执行——校验不误伤。"""
    calls: list[str] = []
    rig.registry.add(_probe_spec("probe"), lambda args, ws, t: calls.append("ran") or _ok(args, ws, t))
    runner = rig.runner()

    outcome = runner.start_and_run(_probe_plan())

    assert outcome.status == "completed"
    assert calls == ["ran"]
