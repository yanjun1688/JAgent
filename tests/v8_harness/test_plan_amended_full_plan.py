"""D-019 · `plan.amended` 必须载完整计划，重放才不丢 then / evidence_requires。

旧载荷只有 `steps`，fold 按 steps 拼一个新 Plan：`then` 与 `evidence_requires`
在第一次 amend 后就静默变回默认值（None / ()），version 与 plan_hash 也与事实不符。
本文件守三件事：

1. 完整计划载荷穿过 `plan.frozen` → `plan.amended` → 重放两次，**逐字段一致**；
2. 旧 `steps`-only 载荷被**显式拒绝**，不静默走旧路径（写入侧与 fold 侧都要拒）；
3. Runner 的修复载荷由生产方把 version +1，plan 里的 version、payload version 与
   plan_hash 三者自洽，重放得到同一计划。

全程没有 planner / 模型调用（修复用例只装配一个测试用 Repairer）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from jagent.contracts import (
    Budget,
    Event,
    EventType,
    EvidenceReq,
    Plan,
    Step,
    Verify,
    plan_to_dict,
)
from jagent.harness.fold import fold
from jagent.harness.repair import CallableRepairer
from jagent.harness.store import EventStore
from jagent.wiring import build_app

from .conftest import RUN_ID, SESSION_ID, STEP_ID, Harness


def _frozen_plan() -> Plan:
    return Plan(
        steps=(
            Step(
                id=STEP_ID,
                tool="probe",
                args={"query": "v1"},
                verify=Verify("ok == true", needs=("ok",)),
            ),
        ),
        version=1,
        then="advice",
        evidence_requires=(EvidenceReq(step_id=STEP_ID, field="ok"),),
    )


def _seed_frozen(harness: Harness, plan: Plan) -> None:
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "gather", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=RUN_ID,
    )
    harness.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": plan.version},
        run_id=RUN_ID,
    )


def _amended(plan: Plan) -> Plan:
    """改 args、升 version；then / evidence_requires 原样带过（由完整载荷保证）。"""
    tampered = dataclasses.replace(plan.steps[0], args={"query": "v2"})
    return dataclasses.replace(plan, steps=(tampered,), version=plan.version + 1)


def _emit_amendment(harness: Harness, plan: Plan, *, invalidated: list[str] | None = None) -> None:
    harness.writer.emit(
        EventType.PLAN_AMENDED,
        {
            "plan": plan_to_dict(plan),
            "plan_hash": plan.hash(),
            "version": plan.version,
            "reason": "assertion_repair",
            "invalidated": invalidated or [],
        },
        run_id=RUN_ID,
    )


def test_amended_plan_keeps_then_evidence_version_and_hash_across_replay(harness: Harness) -> None:
    frozen = _frozen_plan()
    amended = _amended(frozen)
    _seed_frozen(harness, frozen)
    _emit_amendment(harness, amended)

    events = harness.events()
    first = fold(events)
    second = fold(events)

    assert first.fingerprint() == second.fingerprint(), "同一事件流重放两次投影必须一致"

    run = first.runs[RUN_ID]
    assert run.plan is not None
    assert run.plan.then == "advice", "then 在 amend 后丢了"
    assert run.plan.evidence_requires == (EvidenceReq(step_id=STEP_ID, field="ok"),), (
        "evidence_requires 在 amend 后丢了"
    )
    assert run.version == amended.version == 2
    assert run.plan_hash == amended.hash()
    assert run.plan.steps[0].args == {"query": "v2"}, "amended 载荷里的计划没有被应用"

    # 换一个 store 句柄（等价于进程重启）再重放，投影仍然一致。
    other = EventStore(harness.db_path)
    try:
        assert fold(other.read()).fingerprint() == first.fingerprint()
    finally:
        other.close()


def test_steps_only_amendment_is_rejected_by_the_writer(harness: Harness) -> None:
    """旧载荷在写入侧就不合法：必填字段是 plan / plan_hash / version。"""
    _seed_frozen(harness, _frozen_plan())

    with pytest.raises(ValueError, match=r"plan\.amended is missing plan"):
        harness.writer.emit(
            EventType.PLAN_AMENDED,
            {"steps": [], "reason": "operator_edit", "invalidated": []},
            run_id=RUN_ID,
        )


def test_steps_only_amendment_is_rejected_by_fold(harness: Harness) -> None:
    """绕过写入侧的旧日志也必须显式报错，不能静默按 steps 拼装。"""
    _seed_frozen(harness, _frozen_plan())
    events = list(harness.events())
    legacy = Event(
        event_id="legacy_plan_amended",
        seq=events[-1].seq + 1,
        type=EventType.PLAN_AMENDED,
        payload={"steps": [], "reason": "operator_edit", "invalidated": []},
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_id=None,
        at=0.0,
    )

    with pytest.raises(ValueError, match="full plan"):
        fold([*events, legacy])


# --------------------------------------------------------------------------- #
# 4. Runner 的修复载荷：version 由生产方 +1，plan / hash / version 自洽且可重放
# --------------------------------------------------------------------------- #

def test_runner_repair_bumps_version_and_replays_identically(tmp_path: Path) -> None:
    failing = Plan(
        steps=(
            Step(
                id="s1",
                tool="shell.exec",
                args={"cmd": 'python -c "print(1)"'},
                verify=Verify("exit_code == 7", needs=("exit_code",)),
            ),
        ),
    )
    fixed_step = dataclasses.replace(
        failing.steps[0], verify=Verify("exit_code == 0", needs=("exit_code",))
    )
    fixed = dataclasses.replace(failing, steps=(fixed_step,))
    app = build_app(
        tmp_path,
        repairer=CallableRepairer(lambda **_: fixed),
        budget=Budget(max_repairs=1, auto_approve_tools=("shell.exec",)),
    )
    try:
        outcome = app.runner.start_and_run(failing)
        assert outcome.status == "completed", outcome.reason

        run = app.world.runs[outcome.run_id]
        frozen_version = 1
        assert run.version == frozen_version + 1, "修复后 run.version 没有 +1"
        assert run.plan is not None
        assert run.plan.version == run.version, "计划里的 version 与 payload version 不一致"
        assert run.plan_hash == run.plan.hash(), "plan_hash 不是按修订版计划（version+1）算的"

        first = fold(app.store.read())
        second = fold(app.store.read())
        assert first.fingerprint() == second.fingerprint(), "重放两次投影必须一致"
        replayed = first.runs[outcome.run_id]
        assert replayed.version == run.version
        assert replayed.plan_hash == run.plan_hash
        assert replayed.plan == run.plan
    finally:
        app.close()
