"""P1b · C2：修复预算耗尽 → 不再调修复器、不静默重试、报告说清事实。

规格（架构 §4.10）：修复次数有上限，超了 → ``run.failed`` + 报告。
D-027 补充：报告必须写清尝试次数与最后一次失败原因；"转人工"是报告里
给人看的一句话，**不是**审批事件——run 已经是终态，再发 approval.requested
会被终态守卫改写成 run.transition_rejected（I1）。
"""

from __future__ import annotations

from dataclasses import replace

from jagent.contracts import Budget, EventType, Plan, Step, Verify
from tests.v8_repair.support import build_probe_app


def _failing_plan() -> Plan:
    return Plan(
        steps=(
            Step(
                id="s1",
                tool="probe",
                args={"value": 1},
                verify=Verify("value == 2", needs=("value",)),
            ),
        )
    )


class _RecordingRepairer:
    """每次都返回同一份（仍会失败的）计划；记录被调用了几次。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def repair(self, *, plan, step_id, failure, attempt):
        self.calls.append((step_id, attempt))
        return replace(plan, steps=tuple(replace(s) for s in plan.steps))


def _events_of(app, event_type):
    return [event for event in app.store.read() if event.type is event_type]


def test_exhausted_repairs_stop_calling_the_repairer_and_fail_the_run(tmp_path) -> None:
    plan = _failing_plan()
    repairer = _RecordingRepairer()
    app, calls = build_probe_app(
        tmp_path / "app", repairer=repairer, budget=Budget(max_repairs=2)
    )
    try:
        outcome = app.runner.start_and_run(plan)

        assert outcome.status == "failed"
        assert len(repairer.calls) == 2, "预算耗尽后不许再调第三次修复器"
        assert len(calls) == 3, "初次 + 两次修复重跑；不许静默重试"
        assert outcome.reason.startswith("repair_budget_exhausted"), outcome.reason
        assert "attempts=3" in outcome.reason
        assert "assertion_failed" in outcome.reason, "最后一次失败原因必须进失败原因"

        run = app.world.runs[outcome.run_id]
        assert run.status == "failed"
        assert run.budget["repairs"] == 2
        assert run.fail_reason == outcome.reason
        assert run.steps["s1"].attempts == 3

        # 报告/摘要：尝试次数 + 最后失败原因 + 转人工，都是给人看的事实。
        summary = outcome.result.summary
        assert "修复预算已用尽" in summary
        assert "2/2" in summary
        assert "尝试 3 次" in summary
        assert "assertion_failed" in summary
        assert "转人工" in summary

        # 终态之后不许再发审批事件（否则会被 I1 改写成 rejected）。
        assert _events_of(app, EventType.APPROVAL_REQUESTED) == []
        assert len(_events_of(app, EventType.RUN_FAILED)) == 1
        assert _events_of(app, EventType.RUN_TRANSITION_REJECTED) == []
    finally:
        app.close()


def test_zero_repair_budget_never_calls_the_repairer(tmp_path) -> None:
    repairer = _RecordingRepairer()
    app, calls = build_probe_app(
        tmp_path / "app", repairer=repairer, budget=Budget(max_repairs=0)
    )
    try:
        outcome = app.runner.start_and_run(_failing_plan())

        assert outcome.status == "failed"
        assert repairer.calls == [], "max_repairs=0 时一次修复都不该发生"
        assert len(calls) == 1, "只允许初次执行，不许静默重试"
        assert "attempts=1" in outcome.reason
        assert "0/0" in outcome.result.summary
        assert "尝试 1 次" in outcome.result.summary
        assert _events_of(app, EventType.PLAN_AMENDED) == []
        assert _events_of(app, EventType.APPROVAL_REQUESTED) == []
    finally:
        app.close()


def test_replay_of_an_exhausted_repair_keeps_the_report_facts(tmp_path) -> None:
    """耗尽后的报告事实必须只来自事件；重放两次一致（不重新调修复器）。"""

    app, _calls = build_probe_app(
        tmp_path / "app", repairer=_RecordingRepairer(), budget=Budget(max_repairs=1)
    )
    try:
        outcome = app.runner.start_and_run(_failing_plan())
        assert outcome.status == "failed"

        app.reload()
        first = app.world.fingerprint()
        first_result = app.world.runs[outcome.run_id].result
        app.reload()
        second = app.world.fingerprint()

        assert first == second
        assert first_result == app.world.runs[outcome.run_id].result
        assert first_result["summary"] == outcome.result.summary
    finally:
        app.close()
