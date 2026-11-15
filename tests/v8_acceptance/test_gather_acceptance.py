"""v8 验收 #7 / #13 / #15 / #16 / #18（grounding §6）。

只做会话级：``build_app`` 装配 -> ``Session.handle_turn`` -> 读事件。
#17（run 被取消）按 D-020 延期，这里不覆盖；也不重复 gate 的单元测试。
"""

from __future__ import annotations

from jagent.agent.delivery import SYSTEM_SEPARATOR, render
from jagent.agent.propose import ProposeError
from jagent.contracts import (
    Advice,
    Citation,
    EventType,
    EvidenceReq,
    IntentType,
    Plan,
    Step,
    Verify,
)
from jagent.harness.fold import fold
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter

from .support import (
    FakeJudge,
    FakePropose,
    RejectOpinion,
    advice_from_payload,
    empty_model,
    write_doc,
)


def _app(
    tmp_path,
    plan: Plan,
    advice: Advice,
    *,
    judge=None,
    model=None,
    intent: IntentType = IntentType.GATHER,
):
    propose = FakePropose(advice)
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(intent),
        propose=propose,
        judge=judge or FakeJudge(),
        model=model,
    )
    write_doc(app)
    return app, propose


def _read(*, required: bool = True, path: str = "doc.md") -> Step:
    return Step(
        id="read" if required else "optional",
        tool="fs.read",
        args={"path": path},
        required=required,
        verify=Verify("len(content) > 0", needs=("content",)),
    )


def _read_plan(*requires: EvidenceReq) -> Plan:
    return Plan(
        steps=(_read(),),
        then="advice",
        evidence_requires=tuple(requires),
    )


def _delivered(events):
    return [event for event in events if event.type is EventType.ADVICE_DELIVERED]


def _checked(events):
    return [event for event in events if event.type is EventType.GATE_CHECKED]


# --------------------------------------------------------------------------- #
# #7 evidence_requires 少一项 -> 全程 insufficient_evidence + 兜底
# --------------------------------------------------------------------------- #

def test_acceptance_07_missing_one_requirement_is_insufficient_and_falls_back(
    tmp_path,
) -> None:
    advice = Advice(body="文件存在。", refs=(Citation("read", "content"),))
    plan = _read_plan(
        EvidenceReq(step_id="read", field="content"),
        EvidenceReq(step_id="read", field="digest"),
    )
    judge = FakeJudge()
    app, propose = _app(tmp_path, plan, advice, judge=judge)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    # 计划跑完，但取数前声明的证据少了一项：Propose 只能写"缺什么"。
    assert [req.field for req in propose.calls[0]["missing"]] == ["digest"]
    gate = _checked(events)[-1]
    assert gate.payload["mode"] == "insufficient_evidence"
    assert gate.payload["pass"] is False
    assert gate.payload["fallback"] == {"bool": True, "reason": "evidence_incomplete"}
    assert any(v["rule_id"] == "advice.evidence_incomplete" for v in gate.payload["violations"])
    assert judge.requests == [], "T1 已失败时不调用 judge（judge 不能推翻对账）"
    delivered = _delivered(events)[-1]
    assert delivered.payload["mode"] == "insufficient_evidence"
    assert delivered.payload["fallback"]["bool"] is True
    assert "ADVICE_EVIDENCE_INCOMPLETE" in out.text


# --------------------------------------------------------------------------- #
# #13 同一事件流重放两次 -> 文本 / citations / gate 结论一致，零模型
# --------------------------------------------------------------------------- #

def test_acceptance_13_replay_is_stable_and_uses_no_model(tmp_path) -> None:
    advice = Advice(
        body="文件存在，内容非空。",
        risks=("信息风险：只基于本次观测。",),
        refs=(Citation("read", "content", label="文件内容"),),
    )
    model = empty_model()
    app, propose = _app(tmp_path, _read_plan(EvidenceReq("read", "content")), advice, model=model)
    try:
        out = app.session.handle_turn("先看文件再判断")
        # D-042：run 终态会调一次模型写收尾总结（这个替身没有脚本，调用失败后
        # 退回模板摘要）。重放的判据是"此后不再有调用"，不是"全程零调用"。
        calls_after_turn = model.calls
        events = list(app.store.read())
        delivered_before = [dict(e.payload) for e in _delivered(events)]
        checked_before = [dict(e.payload) for e in _checked(events)]

        # 同一事件流折叠两次：投影完全一致。
        first = fold(events)
        second = fold(events)
        assert first.fingerprint() == second.fingerprint()

        app.reload()
        app.reload()
        events_after = list(app.store.read())
    finally:
        app.close()

    # 重放不调用任何模型；也没有重新合成。
    assert model.calls == calls_after_turn, "重放期间发生了模型调用"
    assert len(propose.calls) == 1
    assert [dict(e.payload) for e in _delivered(events_after)] == delivered_before
    assert [dict(e.payload) for e in _checked(events_after)] == checked_before

    # 文本与 citations 可由事件重放：渲染两次逐字相同，且等于当次投递文本。
    payload = delivered_before[-1]["advice"]
    first_text = render(advice_from_payload(payload))
    second_text = render(advice_from_payload(payload))
    assert first_text == second_text == out.text
    assert payload["refs"][0]["seq"] is not None
    assert payload["refs"][0]["seq"] == checked_before[-1]["citations"][0]["seq"]


# --------------------------------------------------------------------------- #
# #15 judge REJECT -> 直接兜底；不重新调用 Propose；理由不进下一轮输入
# --------------------------------------------------------------------------- #

def test_acceptance_15_judge_reject_is_terminal_and_reason_never_reenters(
    tmp_path,
) -> None:
    reason = "REJECT-REASON-NEVER-BACK-TO-PROPOSE"
    advice = Advice(body="文件存在。", refs=(Citation("read", "content"),))
    judge = FakeJudge(RejectOpinion(reason))
    app, propose = _app(tmp_path, _read_plan(EvidenceReq("read", "content")), advice, judge=judge)
    try:
        app.session.handle_turn("先看文件再判断")
        first_calls = len(propose.calls)
        app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert first_calls == 1, "REJECT 之后同一 turn 不得再调用 Propose 重生成"
    assert len(propose.calls) == 2, "第二个 turn 各自只合成一次"
    gates = _checked(events)
    assert gates[0].payload["judge"]["verdict"] == "reject"
    assert gates[0].payload["judge"]["reason"] == reason
    assert gates[0].payload["fallback"] == {"bool": True, "reason": "policy_reject"}
    delivered = _delivered(events)
    assert delivered[0].payload["fallback"] == {"bool": True, "reason": "policy_reject"}
    # 理由只进 gate.checked / 审计，不进下一次 Propose 的输入。
    assert reason not in repr(propose.calls[1])


# --------------------------------------------------------------------------- #
# #16 非必需步失败、必需证据齐 -> 合成；grounded + partial + 系统行
# --------------------------------------------------------------------------- #

def test_acceptance_16_non_required_failure_is_partial_with_system_line(tmp_path) -> None:
    plan = Plan(
        steps=(
            _read(),
            _read(required=False, path="missing.md"),
        ),
        then="advice",
        evidence_requires=(EvidenceReq("read", "content"),),
    )
    model_body = "文件存在，内容非空。"
    advice = Advice(
        body=model_body,
        refs=(Citation("read", "content", label="文件内容"),),
    )
    app, propose = _app(tmp_path, plan, advice)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert len(propose.calls) == 1
    gate = _checked(events)[-1]
    assert gate.payload["mode"] == "grounded"
    assert gate.payload["partial"] is True
    delivered = _delivered(events)[-1]
    assert delivered.payload["mode"] == "grounded"
    assert delivered.payload["partial"] is True

    model_part, system_part = out.text.split(SYSTEM_SEPARATOR, 1)
    assert "1 步未成功" in system_part
    assert "1 步未成功" not in model_part
    assert "1 步未成功" not in model_body


# --------------------------------------------------------------------------- #
# #18 then="advice" 但 steps=() -> 直接 Propose；framework；无 citations
# --------------------------------------------------------------------------- #

def test_acceptance_18_empty_plan_goes_straight_to_propose(tmp_path) -> None:
    advice = Advice(body="只给通用框架。", framework=("先写清目标。",))
    app, propose = _app(tmp_path, Plan(steps=(), then="advice"), advice)
    try:
        out = app.session.handle_turn("给个判断框架")
        events = app.store.read()
    finally:
        app.close()

    assert out.status == "answered"
    assert len(propose.calls) == 1
    assert propose.calls[0]["evidence"].items == ()
    assert propose.calls[0]["missing"] == ()
    assert not [e for e in events if e.type is EventType.RUN_STARTED]
    assert not [e for e in events if e.type is EventType.PLAN_FROZEN]

    gate = _checked(events)[-1]
    assert gate.payload["mode"] == "framework"
    assert gate.payload["violations"] == []
    assert gate.payload["citations"] == []
    delivered = _delivered(events)[-1]
    assert delivered.payload["mode"] == "framework"
    assert delivered.payload["advice"]["refs"] == []
    assert delivered.payload["evidence"]["citations"] == []
    assert "ADVICE_NO_CITATION" not in out.text


# --------------------------------------------------------------------------- #
# E19 触发以意图为准（D-022）
# --------------------------------------------------------------------------- #

def _read_plan_with(then: str | None) -> Plan:
    return Plan(
        steps=(_read(),),
        then=then,
        evidence_requires=(EvidenceReq("read", "content"),),
    )


def _evidence_refs() -> tuple[Citation, ...]:
    return (Citation("read", "content", label="文件内容"),)


def test_e19_trigger_is_intent_when_gather_plan_has_no_then(tmp_path) -> None:
    """GATHER + then=None：意图本身触发 E19，不再依赖计划里多写一个字段。"""

    plan = _read_plan_with(None)
    advice = Advice(body="文件存在，内容非空。", refs=_evidence_refs())
    app, propose = _app(tmp_path, plan, advice)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert out.status == "answered"
    assert len(propose.calls) == 1
    gate = _checked(events)[-1]
    assert gate.payload["trigger"] == "intent"
    assert gate.payload["mode"] == "grounded"
    delivered = _delivered(events)[-1]
    assert delivered.payload["mode"] == "grounded"
    assert delivered.payload["evidence"]["citations"][0]["seq"] is not None


def test_e19_trigger_is_then_for_non_gather_plan(tmp_path) -> None:
    """非 GATHER + then="advice"：显式声明仍可作为补充触发（D-002 的旧条件）。"""

    plan = _read_plan_with("advice")
    advice = Advice(body="文件存在，内容非空。", refs=_evidence_refs())
    app, propose = _app(tmp_path, plan, advice, intent=IntentType.EXECUTE)
    try:
        out = app.session.handle_turn("读一下文件")
        events = app.store.read()
    finally:
        app.close()

    assert out.status == "answered"
    assert len(propose.calls) == 1
    gate = _checked(events)[-1]
    assert gate.payload["trigger"] == "then"
    assert gate.payload["mode"] == "grounded"


def test_e19_does_not_trigger_for_non_gather_plan_without_then(tmp_path) -> None:
    """非 GATHER + then=None：普通执行，没有 E19 合成，也没有 gate.checked。"""

    plan = _read_plan_with(None)
    advice = Advice(body="文件存在，内容非空。", refs=_evidence_refs())
    app, propose = _app(tmp_path, plan, advice, intent=IntentType.EXECUTE)
    try:
        out = app.session.handle_turn("读一下文件")
        events = app.store.read()
    finally:
        app.close()

    assert out.status == "answered"
    assert out.result is not None and out.result.verdict.value == "completed"
    assert propose.calls == []
    assert _checked(events) == []
    # 没有 E19 合成 → 没有建议投递；只有一条 run 收尾复述（D-042 / R2，mode=report）。
    assert [e.payload.get("mode") for e in _delivered(events)] in ([], ["report"])
    # 普通执行只起草一次：收尾复述在独立模块里，不再二次调 Planner（D-004 的
    # `_answer_from_evidence` 已退役——一次 run 一段叙事）。
    assert app.session.planner.calls == 1


class _FailingPropose:
    """Propose 输出不合法：`_compose_advice` 捕获 `ProposeError` 后返回 None。"""

    def __init__(self) -> None:
        self.calls = 0

    def compose(self, **kwargs):
        self.calls += 1
        raise ProposeError("模型输出不是合法 JSON")


def test_e19_compose_failure_does_not_fall_back_to_planner(tmp_path) -> None:
    """F2-01：gather 的 E19 合成失败后，不得回落到 `Planner.plan` 做总结（D-004）。"""

    plan = _read_plan_with(None)
    planner = DraftPlanner([plan])
    propose = _FailingPropose()
    app = build_app(
        tmp_path,
        planner=planner,
        router=FixedRouter(IntentType.GATHER),
        propose=propose,
        judge=FakeJudge(),
    )
    write_doc(app)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert propose.calls == 1, "E19 只尝试一次，且失败后不重试"
    # 初始起草 1 次；没有第二次 `Planner.plan`（`_answer_from_evidence` 未被调用）。
    assert planner.calls == 1
    assert out.status == "answered"
    assert out.result is not None and out.result.verdict.value == "completed"
    assert _checked(events) == []
    assert _delivered(events) == []
    ended = [event for event in events if event.type is EventType.TURN_ENDED][-1]
    assert ended.payload["outcome"] == "handed_off"
