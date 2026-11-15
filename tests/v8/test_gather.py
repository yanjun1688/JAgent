"""Wave 2 · C4 · gather 闭环（E19 / D-002 / D-004）。

只做会话级接线，不改 gate 判定：

* ``then="advice"`` + run 终态 + 所有步 settled -> 用 ``Propose.compose`` 合成；
* 证据来自 ``step.finished``（600 字截断、artifact_ids、failed 步也带上）；
* ``missing`` 只来自 ``jagent.evidence.missing``；``mode`` / ``partial`` 只复制 gate；
* 挂起不合成，恢复后到终态再合成；空计划直接 Propose（不建 run）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
from jagent.agent.delivery import SYSTEM_SEPARATOR
from jagent.harness.judge import JudgeVerdict
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter


class FakeJudge:
    """可返回 REJECT 的 judge 替身；默认放行但标记（UNDECIDABLE）。"""

    model = "fake-judge"

    def __init__(self, outcome: JudgeVerdict | None = JudgeVerdict.UNDECIDABLE) -> None:
        self.outcome = outcome
        self.calls: list[object] = []

    def judge(self, request: object) -> JudgeVerdict | None:
        self.calls.append(request)
        return self.outcome


class FakePropose:
    """记录 compose 入参并返回固定建议；不联网。"""

    def __init__(self, advice: Advice) -> None:
        self.advice = advice
        self.calls: list[dict] = []

    def compose(self, **kwargs) -> Advice:
        self.calls.append(kwargs)
        return self.advice


def _read_plan(*, then: str | None = "advice") -> Plan:
    return Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        ),
        then=then,
        evidence_requires=(EvidenceReq(step_id="read", field="content"),),
    )


def _app(tmp_path: Path, plan: Plan, advice: Advice, *, judge: FakeJudge | None = None):
    propose = FakePropose(advice)
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.GATHER),
        propose=propose,
        judge=judge or FakeJudge(),
    )
    (app.root / "ws" / "doc.md").write_text("hello\n", encoding="utf-8")
    return app, propose


def test_gather_runs_the_plan_then_composes_from_evidence(tmp_path: Path) -> None:
    advice = Advice(
        body="文件存在，内容非空。",
        risks=("信息风险：只基于这一次观测。",),
        framework=("先核对来源。",),
        refs=(Citation(step_id="read", field="content", label="文件内容"),),
    )
    app, propose = _app(tmp_path, _read_plan(), advice)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert out.status == "answered"
    # 计划只起草一次；收尾走 Propose，不复用 Planner.plan（D-004）。
    assert app.session.planner.calls == 1
    assert len(propose.calls) == 1
    call = propose.calls[0]
    assert call["then"] == "advice"
    assert call["missing"] == ()
    assert call["evidence"].run_id
    assert [item.step_id for item in call["evidence"].items] == ["read"]

    gate = [e for e in events if e.type is EventType.GATE_CHECKED][-1]
    assert gate.payload["mode"] == "grounded"
    assert gate.payload["partial"] is False
    assert gate.payload["fallback"] == {"bool": False, "reason": ""}
    assert gate.payload["citations"][0]["seq"] is not None

    delivered = [e for e in events if e.type is EventType.ADVICE_DELIVERED][-1]
    assert delivered.payload["mode"] == "grounded"
    assert delivered.payload["partial"] is False
    assert delivered.payload["advice"]["refs"][0]["seq"] is not None
    assert delivered.payload["evidence"]["run_id"] == call["evidence"].run_id
    assert delivered.payload["evidence"]["citations"][0]["seq"] is not None
    # 文本里的来源行用回填后的 seq 渲染。
    assert "read#" in out.text


def test_missing_required_evidence_falls_back_as_insufficient(tmp_path: Path) -> None:
    # 必需步读不到文件：run failed，证据里没有 content -> missing 一项。
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "nope.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        ),
        then="advice",
        evidence_requires=(EvidenceReq(step_id="read", field="content"),),
    )
    advice = Advice(body="文件内容为空。", refs=(Citation("read", "content"),))
    app, propose = _app(tmp_path, plan, advice)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert len(propose.calls) == 1
    assert [req.field for req in propose.calls[0]["missing"]] == ["content"]
    gate = [e for e in events if e.type is EventType.GATE_CHECKED][-1]
    assert gate.payload["mode"] == "insufficient_evidence"
    assert gate.payload["pass"] is False
    assert gate.payload["fallback"] == {"bool": True, "reason": "evidence_incomplete"}
    delivered = [e for e in events if e.type is EventType.ADVICE_DELIVERED][-1]
    assert delivered.payload["mode"] == "insufficient_evidence"
    assert delivered.payload["fallback"]["bool"] is True
    assert "证据" in out.text or "EVIDENCE" in out.text


def test_non_required_failure_composes_grounded_partial_with_system_line(tmp_path: Path) -> None:
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
            Step(
                id="optional",
                tool="fs.read",
                args={"path": "missing.md"},
                required=False,
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        ),
        then="advice",
        evidence_requires=(EvidenceReq(step_id="read", field="content"),),
    )
    model_body = "文件存在，内容非空。"
    advice = Advice(
        body=model_body,
        refs=(Citation(step_id="read", field="content", label="文件内容"),),
    )
    app, propose = _app(tmp_path, plan, advice)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert len(propose.calls) == 1
    gate = [e for e in events if e.type is EventType.GATE_CHECKED][-1]
    assert gate.payload["mode"] == "grounded"
    assert gate.payload["partial"] is True
    delivered = [e for e in events if e.type is EventType.ADVICE_DELIVERED][-1]
    assert delivered.payload["mode"] == "grounded"
    assert delivered.payload["partial"] is True
    # 系统行在模型正文之外：模型输出里没有，投递文本的系统段里有。
    model_part, system_part = out.text.split(SYSTEM_SEPARATOR, 1)
    assert "1 步未成功" in system_part
    assert "1 步未成功" not in model_part
    assert "1 步未成功" not in model_body


def test_judge_reject_falls_back_without_regenerating(tmp_path: Path) -> None:
    advice = Advice(
        body="文件存在。",
        refs=(Citation(step_id="read", field="content", label="文件内容"),),
    )
    app, propose = _app(tmp_path, _read_plan(), advice, judge=FakeJudge(JudgeVerdict.REJECT))
    try:
        app.session.handle_turn("先看文件再判断")
        events = app.store.read()
    finally:
        app.close()

    assert len(propose.calls) == 1, "REJECT 是终局，不重生成"
    gate = [e for e in events if e.type is EventType.GATE_CHECKED][-1]
    assert gate.payload["judge"]["verdict"] == "reject"
    assert [v["code"] for v in gate.payload["violations"]] == ["policy_reject"]
    delivered = [e for e in events if e.type is EventType.ADVICE_DELIVERED][-1]
    assert delivered.payload["mode"] == "grounded"
    assert delivered.payload["fallback"] == {"bool": True, "reason": "policy_reject"}


def test_empty_plan_goes_straight_to_propose_without_a_run(tmp_path: Path) -> None:
    plan = Plan(steps=(), then="advice")
    advice = Advice(body="只给通用框架。", framework=("先写清目标。",))
    app, propose = _app(tmp_path, plan, advice)
    try:
        out = app.session.handle_turn("给个框架")
        events = app.store.read()
    finally:
        app.close()

    assert out.status == "answered"
    assert len(propose.calls) == 1
    assert not [e for e in events if e.type is EventType.RUN_STARTED]
    assert not [e for e in events if e.type is EventType.PLAN_FROZEN]
    gate = [e for e in events if e.type is EventType.GATE_CHECKED][-1]
    assert gate.payload["mode"] == "framework"
    assert gate.payload["violations"] == []
    delivered = [e for e in events if e.type is EventType.ADVICE_DELIVERED][-1]
    assert delivered.payload["mode"] == "framework"
    assert delivered.payload["advice"]["refs"] == []
    assert delivered.payload["evidence"]["citations"] == []


def test_pending_gather_is_composed_only_after_resume(tmp_path: Path) -> None:
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "doc.md"},
                gated=True,  # 人工确认：先挂起
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        ),
        then="advice",
        evidence_requires=(EvidenceReq(step_id="read", field="content"),),
    )
    advice = Advice(
        body="文件存在。",
        refs=(Citation(step_id="read", field="content", label="文件内容"),),
    )
    app, propose = _app(tmp_path, plan, advice)
    try:
        first = app.session.handle_turn("先看文件再判断")
        assert first.status == "waiting_human"
        assert propose.calls == [], "挂起时不合成"
        resumed = app.session.resume_run(first.run_id, "approve")
        events = app.store.read()
    finally:
        app.close()

    assert resumed.status == "answered"
    assert len(propose.calls) == 1
    delivered = [e for e in events if e.type is EventType.ADVICE_DELIVERED]
    assert len(delivered) == 1


@pytest.mark.parametrize("omitted", [0, 2])
def test_evidence_bundle_truncates_observations_and_counts_omitted(
    tmp_path: Path, omitted: int
) -> None:
    count = 8 + omitted
    plan = Plan(
        steps=tuple(
            Step(
                id=f"s{i}",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            )
            for i in range(count)
        ),
        then="advice",
    )
    app, _ = _app(
        tmp_path,
        plan,
        Advice(body="只给框架。"),
        judge=FakeJudge(None),
    )
    run_id = "run_evidence"
    try:
        for i in range(count):
            long_value = "x" * 700 if i == count - 1 else "ok"
            app.writer.emit(
                EventType.STEP_FINISHED,
                {
                    "step_id": f"s{i}",
                    "status": "failed" if i == 0 else "ok",
                    "artifact_refs": [f"art-{i}"],
                    "observations": {"content": long_value},
                },
                run_id=run_id,
                step_id=f"s{i}",
            )
        bundle = app.session._evidence_bundle(run_id, plan)
    finally:
        app.close()

    assert bundle.omitted_steps == omitted
    assert len(bundle.items) == 8
    assert bundle.items[-1].truncated == ("content",)
    assert len(bundle.items[-1].observations["content"]) == 600
    assert bundle.items[-1].artifact_ids == (f"art-{count - 1}",)
    assert bundle.items[-1].seq > 0
    # failed 步也进证据（partial 需要它）。
    if omitted == 0:
        assert bundle.items[0].status == "failed"


def test_failed_run_with_pending_downstream_does_not_compose(tmp_path: Path) -> None:
    """C4-01 探针：a passed / b failed / c pending -> 不算 settled，不合成。"""
    plan = Plan(
        steps=(
            Step(
                id="a",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
            Step(
                id="b",
                tool="fs.read",
                args={"path": "missing.md"},
                depends_on=("a",),
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
            Step(
                id="c",
                tool="fs.read",
                args={"path": "doc.md"},
                depends_on=("b",),
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        ),
        then="advice",
        evidence_requires=(EvidenceReq("a", "content"),),
    )
    advice = Advice(body="只给框架。", refs=(Citation("a", "content", label="文件内容"),))
    app, propose = _app(tmp_path, plan, advice)
    try:
        out = app.session.handle_turn("先看文件再判断")
        events = app.store.read()
        run = next(iter(app.world.runs.values()))
        statuses = {step_id: state.status for step_id, state in run.steps.items()}
    finally:
        app.close()

    assert statuses == {"a": "passed", "b": "failed", "c": "pending"}
    assert propose.calls == [], "还有 pending 步时 E19 不合成"
    # 收尾投递（D-042 / R2）不是"建议"：失败的 run 只投一条 mode=report 的复述，
    # 绝不合成 advice、也不过 gate。判据按 mode 区分，不按事件类型一刀切。
    modes = [e.payload.get("mode") for e in events if e.type is EventType.ADVICE_DELIVERED]
    assert modes in ([], ["report"]), f"失败 run 不该合成建议：{modes}"
    assert not [e for e in events if e.type is EventType.GATE_CHECKED]
    # run 仍按普通失败路径收尾，不回建议。
    assert out.status == "answered"
    assert out.result is not None and out.result.verdict.value == "failed"
