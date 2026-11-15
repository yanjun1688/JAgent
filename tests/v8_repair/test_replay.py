"""P1b · C3：修复后的事件流必须可零模型重放。

真验收（§3.3 / §4.10 / D-019 / D-027）：

1. 事件流必须天然包含 ``assertion failure → plan.amended → completed``；
2. 同一流重放两次：World 指纹与报告完全一致；
3. 重放期间模型端口被下毒（任何调用都抛）：重放仍然完成，证明零模型；
4. ``plan.amended`` 载的是完整计划，重放能原样重建 ``then`` / ``evidence_requires``
   / version / plan_hash（D-019），不是只拼回"变更的那一步"。
"""

from __future__ import annotations

from jagent.agent.llm_repair import LLMRepairer
from jagent.agent.model import ScriptedModel
from jagent.contracts import (
    Budget,
    EvidenceReq,
    EventType,
    Plan,
    Step,
    Verify,
    plan_to_dict,
)
from jagent.harness.fold import fold
from jagent.harness.store import EventStore
from tests.v8_repair.support import build_probe_app


def test_repaired_run_replays_twice_with_zero_model_calls(tmp_path, monkeypatch) -> None:
    failing = Plan(
        steps=(
            Step(
                id="s1",
                tool="probe",
                args={"value": 1},
                verify=Verify("value == 2", needs=("value",)),
            ),
        ),
        then="advice",
        evidence_requires=(EvidenceReq(step_id="s1", field="value"),),
    )
    fixed = Plan(
        steps=(
            Step(
                id="s1",
                tool="probe",
                args={"value": 2},
                verify=Verify("value == 2", needs=("value",)),
            ),
        ),
        then="advice",
        evidence_requires=(EvidenceReq(step_id="s1", field="value"),),
    )
    model = ScriptedModel([plan_to_dict(fixed)])
    app, calls = build_probe_app(
        tmp_path / "app", repairer=LLMRepairer(model), budget=Budget(max_repairs=2)
    )
    try:
        outcome = app.runner.start_and_run(failing)
        assert outcome.status == "completed", outcome.reason
        assert len(calls) == 2, "一次失败执行 + 一次修复后执行"
        assert len(model.calls) == 1, "修复只调一次模型"

        events = app.store.read()
        types = [event.type for event in events]
        assert EventType.FAILURE_CLASSIFIED in types
        assert EventType.PLAN_AMENDED in types
        assert EventType.RUN_COMPLETED in types
        assert types.index(EventType.FAILURE_CLASSIFIED) < types.index(EventType.PLAN_AMENDED)
        assert types.index(EventType.PLAN_AMENDED) < types.index(EventType.RUN_COMPLETED)

        run_id = outcome.run_id
        live = app.world.runs[run_id]
        model_calls_before = len(model.calls)

        # 下毒：重放期间任何模型调用 / 修复调用都必须炸，而不是静默降级。
        def _poisoned(*_args, **_kwargs):
            raise AssertionError("replay must not call a model or a repairer")

        monkeypatch.setattr(model, "complete", _poisoned)
        monkeypatch.setattr(LLMRepairer, "repair", _poisoned)

        app.reload()
        first = app.world.fingerprint()
        first_result = app.reporter.render(app.world.runs[run_id])
        app.reload()
        second = app.world.fingerprint()
        second_result = app.reporter.render(app.world.runs[run_id])

        assert first == second, "同一事件流重放两次必须得到同一个 World"
        assert first_result == second_result, "报告必须可重放且一致"
        assert len(model.calls) == model_calls_before, "重放期间零模型调用"

        replayed = app.world.runs[run_id]
        assert replayed.status == "completed"
        assert replayed.plan is not None
        assert replayed.plan == live.plan, "plan.amended 必须够重建完整计划"
        assert replayed.version == live.version == 2
        assert replayed.plan_hash == live.plan_hash == live.plan.hash()
        assert replayed.plan.then == "advice"
        assert replayed.plan.evidence_requires == (EvidenceReq(step_id="s1", field="value"),)
        assert replayed.steps["s1"].status == "passed"
        assert replayed.steps["s1"].attempts == 2
        assert replayed.result is not None
        assert replayed.result["summary"] == first_result.summary
        assert replayed.result["verdict"] == "completed"

        # 换一个 store 句柄（等价于进程重启）再重放，投影仍然一致。
        other = EventStore(app.store.path)
        try:
            restarted = fold(other.read())
        finally:
            other.close()
        assert restarted.fingerprint() == first

        amended = [event for event in events if event.type is EventType.PLAN_AMENDED]
        assert len(amended) == 1
        assert amended[0].payload["plan"] == plan_to_dict(live.plan)
        assert amended[0].payload["invalidated"] == ["s1"]
        assert amended[0].payload["plan"]["then"] == "advice"
        assert amended[0].payload["plan"]["evidence_requires"] == [
            {"step_id": "s1", "field": "value"}
        ]
    finally:
        app.close()
