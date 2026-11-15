"""Wave 2 · C2 · 模型层 outage 的消费（Integration-02 / D-012）。

模型不可用不是"草稿不合格"：

* 只调用一次 planner，不重试；
* 不落 ``plan.rejected``（不计 plan_rejects）；
* ``TurnOutcome.error_code == "model_unavailable"``；
* ``turn.ended{outcome:"model_unavailable"}``。
"""

from __future__ import annotations

from pathlib import Path

from jagent.agent.llm_planner import PlanDraftError
from jagent.contracts import IntentType, Plan, Step, Verify
from jagent.harness.plancheck import PlanIssue
from jagent.wiring import build_app
from tests.support import FixedRouter


def _one_step_plan() -> Plan:
    return Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        )
    )


class OutagePlanner:
    """draft_capable 的 planner：每次调用都抛模型层 outage，并计数。"""

    draft_capable = True

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, **kwargs):
        self.calls += 1
        raise PlanDraftError("模型不可用：配额耗尽", outage=True)


class MessageOnlyPlanner:
    """异常文案写着"模型不可用"，但 outage=False：必须按普通草稿错误处理。"""

    draft_capable = True

    def __init__(self) -> None:
        self.calls = 0
        self.feedbacks: list[list] = []

    def plan(self, **kwargs):
        self.calls += 1
        self.feedbacks.append(list(kwargs.get("feedback") or ()))
        if self.calls == 1:
            raise PlanDraftError(
                "模型不可用：但这只是文案，不是 outage 标志",
                issues=[PlanIssue("BAD", "", "坏草稿")],
            )
        return _one_step_plan()


def test_outage_costs_exactly_one_planner_call_and_no_plan_rejected(tmp_path: Path) -> None:
    planner = OutagePlanner()
    app = build_app(
        tmp_path,
        planner=planner,
        router=FixedRouter(IntentType.EXECUTE),
    )
    try:
        out = app.session.handle_turn("读一下 doc.md")
        events = app.store.read()
    finally:
        app.close()

    assert planner.calls == 1, "outage 必须立刻退出，不许重试"
    assert out.status == "answered"
    assert out.error_code == "model_unavailable"
    assert out.text == ("模型暂时不可用（配额、网络或鉴权），这次没能开始。稍后再试。")
    assert [e for e in events if e.type.value == "plan.rejected"] == []
    assert not [e for e in events if e.type.value == "run.started"]
    ended = [e for e in events if e.type.value == "turn.ended"][-1]
    assert ended.payload["outcome"] == "model_unavailable"


def test_outage_is_the_flag_not_the_message(tmp_path: Path) -> None:
    # 同一个异常类型（PlanDraftError），只有 outage=True 才走 outage 出口。
    planner = MessageOnlyPlanner()
    app = build_app(
        tmp_path,
        planner=planner,
        router=FixedRouter(IntentType.EXECUTE),
    )
    try:
        out = app.session.handle_turn("读一下 doc.md")
        rejected = [e for e in app.store.read() if e.type.value == "plan.rejected"]
    finally:
        app.close()

    assert planner.calls == 2, "没有 outage 标志的草稿错误仍然走 Session 重试"
    assert len(rejected) == 1, "普通草稿错误仍要留痕"
    assert planner.feedbacks[1] and planner.feedbacks[1][0].code == "BAD"
    assert out.error_code != "model_unavailable"


def test_first_round_issues_appear_in_the_next_planner_feedback(tmp_path: Path) -> None:
    """C2-02：第一轮被拒的 issues 必须原样进下一轮 planner 的 ``feedback``。"""
    planner = MessageOnlyPlanner()
    app = build_app(
        tmp_path,
        planner=planner,
        router=FixedRouter(IntentType.EXECUTE),
    )
    try:
        app.session.handle_turn("读一下 doc.md")
    finally:
        app.close()

    assert planner.calls == 2
    assert planner.feedbacks[0] == []
    second = planner.feedbacks[1]
    assert [issue.code for issue in second] == ["BAD"]
    assert second[0].detail == "坏草稿"
