"""I9 · 幂等：同一 approval token 只能生效一次；恢复不能重跑已完成的步。

规格依据：
- §4.12「幂等 token：同一确认重复到达不重复执行；重复的落一条 duplicate: true」；
- §4.12「delta 注入：拒绝 → 剪枝（必需的步骤被拒则 run failed）」；
- §4.10「复用键 = (step_id, args_hash, 上游产物 hash)；输入没变的已通过步骤直接复用」；
- contracts.ToolSpec 注释：``Idempotency.NONE`` 不可幂等，必须 gated。
"""

from __future__ import annotations

import pytest

from jagent.contracts import (
    EventType,
    Idempotency,
    Plan,
    SideEffect,
    Step,
    ToolResult,
    ToolSpec,
    Verify,
    plan_to_dict,
)
from jagent.harness.human import HumanCenter


def _run_with_gated_step(harness, *, plan: Plan | None = None) -> str:
    run_id = "run_gate"
    plan = plan or Plan(
        steps=(Step(id="write", tool="probe", verify=Verify("ok", needs=("ok",)), gated=True),)
    )
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=run_id,
    )
    harness.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=run_id,
    )
    return run_id


def _approval_resolved(harness):
    return [event for event in harness.events() if event.type is EventType.APPROVAL_RESOLVED]


def test_same_token_resolved_twice_second_returns_false(harness) -> None:
    run_id = _run_with_gated_step(harness)
    human = HumanCenter(harness.writer)
    token = human.request(run_id, "write", payload_hash="h")

    assert human.resolve(run_id, token, "approve") is True
    assert human.resolve(run_id, token, "approve") is False

    resolved = _approval_resolved(harness)
    assert [event.payload["applied"] for event in resolved] == [True, False]
    assert resolved[1].payload["duplicate"] is True
    assert len([event for event in harness.events() if event.type is EventType.APPROVAL_RECEIVED]) == 1


def test_unknown_token_is_rejected_without_executing(harness) -> None:
    run_id = _run_with_gated_step(harness)
    human = HumanCenter(harness.writer)
    human.request(run_id, "write", payload_hash="h")

    assert human.resolve(run_id, "token-from-nowhere", "approve") is False

    resolved = _approval_resolved(harness)[-1]
    assert resolved.payload["applied"] is False
    assert resolved.payload["duplicate"] is True


def test_expired_token_is_refused_and_recorded(harness) -> None:
    run_id = _run_with_gated_step(harness)
    clock = [100.0]
    human = HumanCenter(harness.writer, ttl_s=10, clock=lambda: clock[0])
    token = human.request(run_id, "write", payload_hash="h")

    clock[0] = 1000.0
    assert human.resolve(run_id, token, "approve") is False

    resolved = _approval_resolved(harness)[-1]
    assert resolved.payload["applied"] is False
    assert resolved.payload["reason"] == "expired"


def test_unknown_decision_never_pollutes_the_event_log(harness) -> None:
    run_id = _run_with_gated_step(harness)
    human = HumanCenter(harness.writer)
    token = human.request(run_id, "write", payload_hash="h")
    before = len(harness.events())

    with pytest.raises(ValueError):
        human.resolve(run_id, token, "maybe")

    assert len(harness.events()) == before


def test_new_request_supersedes_old_token(harness) -> None:
    run_id = _run_with_gated_step(harness)
    human = HumanCenter(harness.writer)
    first = human.request(run_id, "write", payload_hash="h")
    second = human.request(run_id, "write", payload_hash="h")

    assert first != second
    assert human.resolve(run_id, first, "approve") is False
    assert human.resolve(run_id, second, "approve") is True


def test_resume_does_not_reexecute_a_passed_step(harness) -> None:
    calls: list[int] = []

    def counting(args, workspace, timeout):
        calls.append(1)
        return ToolResult(ok=True)

    spec = ToolSpec(
        name="probe",
        version="1",
        args_schema={"type": "object"},
        side_effect=SideEffect.REVERSIBLE,
        idempotency=Idempotency.KEYED,
        observations=("ok",),
    )
    from .conftest import make_runner

    human = HumanCenter(harness.writer)
    runner = make_runner(harness, tools=[(spec, counting)], human=human)
    plan = Plan(
        steps=(Step(id="write", tool="probe", verify=Verify("ok", needs=("ok",)), gated=True),)
    )

    started = runner.start_and_run(plan)
    assert started.status == "waiting_human"

    token = harness.world.runs[started.run_id].approval["token"]
    assert human.resolve(started.run_id, token, "approve") is True
    resumed = runner.resume(started.run_id, approved_steps=frozenset({"write"}))

    assert resumed.status == "completed"
    assert len(calls) == 1, "同一步只允许执行一次"

    runner.resume(started.run_id, approved_steps=frozenset({"write"}))
    assert len(calls) == 1, "再次恢复不得重复执行已 passed 的步"
    assert harness.world.runs[started.run_id].steps["write"].attempts == 1


def test_approval_request_carries_the_command(harness) -> None:
    """已修复（本轮）：approval.requested.context 现在带 tool + args（人能看到命令）。"""
    from .conftest import make_runner, probe_spec

    spec = probe_spec("shell.exec", observations=("ok", "exit_code"))

    def effect(args, workspace, timeout):
        return ToolResult(ok=True, exit_code=0)

    runner = make_runner(harness, tools=[(spec, effect)])
    plan = Plan(
        steps=(
            Step(
                id="danger",
                tool="shell.exec",
                args={"cmd": "echo UNIQUE-MARKER-COMMAND"},
                verify=Verify("exit_code == 0", needs=("exit_code",)),
                gated=True,
            ),
        )
    )
    runner.start_and_run(plan)

    payload = [
        event for event in harness.events() if event.type is EventType.APPROVAL_REQUESTED
    ][-1].payload
    import json

    assert "UNIQUE-MARKER-COMMAND" in json.dumps(payload, ensure_ascii=False)


def test_rejecting_a_required_step_does_not_reask_forever(harness) -> None:
    from .conftest import make_runner

    executed: list[int] = []

    def effect(args, workspace, timeout):
        executed.append(1)
        return ToolResult(ok=True)

    spec = ToolSpec(
        name="probe",
        version="1",
        args_schema={"type": "object"},
        side_effect=SideEffect.IRREVERSIBLE,
        idempotency=Idempotency.NONE,
        observations=("ok",),
    )
    human = HumanCenter(harness.writer)
    runner = make_runner(harness, tools=[(spec, effect)], human=human)
    plan = Plan(
        steps=(Step(id="danger", tool="probe", verify=Verify("ok", needs=("ok",)), gated=True),)
    )

    started = runner.start_and_run(plan)
    token = harness.world.runs[started.run_id].approval["token"]
    assert human.resolve(started.run_id, token, "reject") is True
    outcome = runner.resume(started.run_id, approved_steps=frozenset())

    assert executed == []
    assert outcome.status == "failed", "必需步骤被拒，run 必须失败，不能重新挂起等人"
    assert harness.world.runs[started.run_id].approval is None
