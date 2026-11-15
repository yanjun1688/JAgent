"""v8 验收 · judge 标记四态（grounding §3 T2 / D-021）。

会话级：``build_app`` -> ``Session.handle_turn`` -> 读 ``gate.checked``。
四态固定为 ``reject`` / ``undecidable`` / ``unavailable`` / ``none``；
``none``（没配 judge）与 ``unavailable``（配了但调不通）都放行但标"未经语义评审"，
``undecidable`` / ``reject`` 是真实评审结论，不标。
"""

from __future__ import annotations

from jagent.agent.delivery import SYSTEM_SEPARATOR
from jagent.contracts import Advice, AdviceDraft, IntentType
from jagent.gate.rules import AdviceGate
from jagent.harness.judge import JudgeVerdict
from jagent.ports import ModelUnavailable
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter

from .support import FakeJudge

_MARK = "（未经语义评审）"


class _UnavailableJudge:
    """配了但调不通：真实 ``LLMJudge`` 会把 ``ModelUnavailable`` 原样上抛。"""

    model = "fake-unavailable"

    def __init__(self) -> None:
        self.calls = 0

    def judge(self, request: object) -> None:
        self.calls += 1
        raise ModelUnavailable("judge endpoint returned 503")


def _run(tmp_path, gate):
    draft = AdviceDraft(advice=Advice(body="只给通用框架，不给具体结论。"))
    app = build_app(
        tmp_path,
        planner=DraftPlanner([draft]),
        router=FixedRouter(IntentType.ADVISE),
        gate=gate,
    )
    try:
        out = app.session.handle_turn("帮我看看这个")
        events = app.store.read()
    finally:
        app.close()
    checked = [event for event in events if event.type.value == "gate.checked"][-1]
    delivered = [event for event in events if event.type.value == "advice.delivered"][-1]
    return out, checked, delivered


def test_judge_unavailable_is_recorded_and_marked(tmp_path) -> None:
    judge = _UnavailableJudge()
    out, checked, delivered = _run(tmp_path, AdviceGate(judge=judge))

    assert judge.calls == 1
    assert checked.payload["pass"] is True
    assert checked.payload["judge"]["verdict"] == "unavailable"
    assert checked.payload["judge"]["reason"] == "judge_unavailable"
    assert delivered.payload["mode"] == "gated"
    assert _MARK in delivered.payload["advice"]["disclaimer"]
    assert _MARK not in out.text.split(SYSTEM_SEPARATOR, 1)[0]


def test_judge_not_configured_is_recorded_as_none_and_marked(tmp_path) -> None:
    _out, checked, delivered = _run(tmp_path, AdviceGate())

    assert checked.payload["pass"] is True
    assert checked.payload["judge"]["verdict"] == "none"
    assert checked.payload["judge"]["reason"] == "judge_not_configured"
    assert delivered.payload["mode"] == "gated"
    assert _MARK in delivered.payload["advice"]["disclaimer"]


def test_judge_undecidable_is_recorded_but_not_marked(tmp_path) -> None:
    _out, checked, delivered = _run(
        tmp_path, AdviceGate(judge=FakeJudge(JudgeVerdict.UNDECIDABLE))
    )

    assert checked.payload["pass"] is True
    assert checked.payload["judge"]["verdict"] == "undecidable"
    assert checked.payload["judge"]["reason"] == "no_opinion"
    assert delivered.payload["mode"] == "gated"
    assert _MARK not in delivered.payload["advice"]["disclaimer"]


def test_judge_reject_is_terminal_and_not_marked(tmp_path) -> None:
    _out, checked, delivered = _run(
        tmp_path, AdviceGate(judge=FakeJudge(JudgeVerdict.REJECT))
    )

    assert checked.payload["pass"] is False
    assert checked.payload["judge"]["verdict"] == "reject"
    assert checked.payload["fallback"] == {"bool": True, "reason": "policy_reject"}
    assert delivered.payload["mode"] == "fallback"
    assert _MARK not in delivered.payload["advice"]["disclaimer"]
