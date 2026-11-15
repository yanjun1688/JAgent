"""I3 · 拒绝留痕：计划/建议被拒也必须是一条可查的事实。

规格依据：
- §2 第 3 条「非法转移直接拒绝（拒绝本身也落事件）」；
- §3.2 `plan.rejected` / `gate.checked{pass:false}` 在最小事件集里；
- §4.4「通过也要写事件，否则重放时无法解释当时为什么放行」；
- docs/jagent-v8-mvp.md §4「拒绝也是事实」。
"""

from __future__ import annotations


from jagent.agent.planner import ScriptedPlanner
from jagent.contracts import Advice, AdviceDraft, EventType, IntentType, Plan, Step, Verify
from jagent.gate.rules import AdviceGate
from jagent.harness.judge import JudgeVerdict
from tests.support import DraftPlanner, FixedRouter


def _events(app):
    return app.store.read()


def _rejected(app):
    return [event for event in _events(app) if event.type is EventType.PLAN_REJECTED]


def test_bad_plan_from_non_draft_planner_dies_in_execution(app_factory) -> None:
    """ScriptedPlanner 绕过草稿自检：坏计划进 Runner，由执行/判定自然淘汰。"""

    bad = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "x.txt"},
                verify=Verify("len(content) > 0 and secret == 1", needs=("content", "secret")),
            ),
        )
    )
    app = app_factory("reject-needs", planner=ScriptedPlanner([bad]), router=FixedRouter())

    outcome = app.session.handle_turn("跑回归测试", {"repo_path": ".", "target": "tests"})

    assert outcome.status == "answered"
    assert outcome.run_id is not None
    assert app.world.runs[outcome.run_id].status == "failed"
    assert not _rejected(app), "没有前置计划门，不该出现 plan.rejected"
    types = [event.type for event in _events(app)]
    assert EventType.RUN_STARTED in types
    assert EventType.PLAN_FROZEN in types
    assert EventType.RUN_FAILED in types


def test_unknown_tool_plan_dies_in_execution(app_factory) -> None:
    bad = Plan(
        steps=(
            Step(
                id="traverse",
                tool="../../etc/passwd",
                args={},
                verify=Verify("ok", needs=("ok",)),
            ),
        )
    )
    app = app_factory("reject-tool", planner=ScriptedPlanner([bad]), router=FixedRouter())

    outcome = app.session.handle_turn("跑回归测试", {"repo_path": ".", "target": "tests"})

    assert outcome.status == "answered"
    assert outcome.run_id is not None
    assert app.world.runs[outcome.run_id].status == "failed"
    assert outcome.result is not None
    assert outcome.result.steps[0].status == "failed"
    assert not _rejected(app)


def test_run_failure_is_recorded_before_turn_ended(app_factory) -> None:
    bad = Plan(
        steps=(
            Step(
                id="no-verify",
                tool="fs.list",
                args={"path": "."},
                verify=None,
            ),
        )
    )
    app = app_factory("reject-order", planner=ScriptedPlanner([bad]), router=FixedRouter())

    app.session.handle_turn("跑回归测试", {"repo_path": ".", "target": "tests"})

    sequence = [(event.seq, event.type) for event in _events(app)]
    failed_seq = next(seq for seq, kind in sequence if kind is EventType.RUN_FAILED)
    ended_seq = next(seq for seq, kind in sequence if kind is EventType.TURN_ENDED)
    assert failed_seq < ended_seq, "先落 run.failed，再结束 turn"


def test_gate_failure_is_recorded(app_factory) -> None:
    """建议被合规拦下时，gate.checked{pass:false} 必须在日志里。

    Propose 组件已删除：建议由 planner 的 advice 草稿产出，闸门不变。
    v8.2：T1 只对账，"建议买入"这类语义否决由 T2 judge 给出（policy_reject）。
    """

    class RejectJudge:
        model = "fake-judge"

        def judge(self, request):
            return JudgeVerdict.REJECT

    risky = Advice(
        body="建议买入这只标的。",
        risks=("风险一", "风险二"),
        disclaimer="不构成投资建议。",
        framework=(),
    )

    app = app_factory(
        "gate-trace",
        planner=DraftPlanner([AdviceDraft(advice=risky)]),
        router=FixedRouter(IntentType.ADVISE),
        gate=AdviceGate(judge=RejectJudge()),
    )
    outcome = None
    for _ in range(4):
        outcome = app.session.handle_turn("这支股票值得买吗", {})
        if outcome.status != "needs_input":
            break

    checked = [event for event in _events(app) if event.type is EventType.GATE_CHECKED]
    assert checked, "gate.checked 必须落盘（通过/不通过都要）"
    assert checked[-1].payload["pass"] is False
    assert checked[-1].payload["profile"] == "advice"
    assert checked[-1].payload["violations"]
    assert checked[-1].payload["ruleset"]


def test_model_plan_draft_failure_is_traced(app_factory) -> None:
    """模型草稿连续被拒：每稿都落 plan.rejected，耗尽后 turn 失败但不建 run。"""

    from jagent.agent.llm_planner import LLMPlanner
    from jagent.agent.model import ScriptedModel

    app = app_factory("llm-reject")
    bad_reply = '{"steps": []}'
    app.session.planner = LLMPlanner(ScriptedModel([bad_reply] * 3), app.registry)

    outcome = app.session.handle_turn("跑回归测试", {"repo_path": ".", "target": "tests"})

    assert outcome.status == "answered"
    assert outcome.error_code == "plan_rejected"
    assert outcome.run_id is None
    assert app.world.runs == {}, "草稿被拒不建 run"
    rejected = _rejected(app)
    assert len(rejected) == 3
    assert [event.payload["attempt"] for event in rejected] == [1, 2, 3]
    assert all(event.payload["draft_artifact_id"] for event in rejected)
    assert rejected[0].payload["issues"]
    assert app.executor.artifacts.get_text(rejected[0].payload["draft_artifact_id"]) == bad_reply
    ended = [event for event in _events(app) if event.type is EventType.TURN_ENDED]
    assert ended and ended[-1].payload["outcome"] == "plan_rejected"
    types = [event.type for event in _events(app)]
    assert EventType.RUN_STARTED not in types
    assert EventType.PLAN_FROZEN not in types


def test_plan_proposed_records_the_real_attempt(app_factory) -> None:
    """第一稿被拒、第二稿通过：plan.proposed.attempt 必须是 2，不是写死的 1。"""

    from jagent.agent.llm_planner import LLMPlanner
    from jagent.agent.model import ScriptedModel

    app = app_factory("llm-attempt", router=FixedRouter())
    good = (
        '{"steps":[{"id":"read","tool":"fs.read","args":{"path":"doc.md"},'
        '"depends_on":[],"verify":{"expr":"ok == true","needs":["ok"]}}]}'
    )
    app.session.planner = LLMPlanner(
        ScriptedModel(['{"steps": []}', good]), app.registry
    )

    app.session.handle_turn("读一下 doc.md")

    rejected = _rejected(app)
    assert len(rejected) == 1
    assert rejected[0].payload["attempt"] == 1
    proposed = [event for event in _events(app) if event.type is EventType.PLAN_PROPOSED]
    assert proposed and proposed[-1].payload["attempt"] == 2
