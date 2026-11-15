"""JAgent v8 · LLM 链路的端到端测试（我写的，不是实现者写的）。

证明两件事：
1. 接上模型之后，整条 turn → plan → execute → verify → 报告 仍然成立；
2. 模型犯浑（不对的 JSON、不存在的工具）时，系统给用户一句人话，而不是崩掉或假装成功。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jagent.contracts import Budget
from jagent.harness.judge import JudgeVerdict
from jagent.ports import NoModel
from jagent.wiring import build_app
from jagent.app.session import PATH_SLOT

POOL = {
    "repo_path": ".",
    "target": ".",
    "ticker": "示例标的",
    "horizon": "1年",
    "risk_profile": "均衡",
    "capital": "10万以内",
}


class FakeJudge:
    """可返回 REJECT 的 T2 替身（C1 硬约束：不许用永远 UNDECIDABLE 的桩）。

    默认 ``None``（没有意见）只用于**不会走到 advice 闸门**的用例；
    真正过闸的用例显式选择结论。
    """

    model = "fake-judge"

    def __init__(self, outcome: JudgeVerdict | None = None) -> None:
        self.outcome = outcome
        self.calls: list[object] = []

    def judge(self, request: object) -> JudgeVerdict | None:
        self.calls.append(request)
        return self.outcome


class DispatchModel:
    """按 prompt 里出现的 JSON 键分发，避免依赖中文提示词。"""

    def __init__(self, *, router: str = "", planner: str = "", proposer: str = "",
                 reporter: str = "已完成读取。"):
        self.replies = {
            "router": router, "planner": planner, "proposer": proposer, "reporter": reporter,
        }
        self.calls: list[str] = []

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
        from jagent.ports import ModelReply

        blob = system + prompt
        # 收尾总结（D-042）先认系统提示词：它的事实块里也有 "steps" 键。
        if "收尾总结" in system:
            kind = "reporter"
        elif '"steps"' in blob:
            kind = "planner"
        elif '"risks"' in blob:
            kind = "proposer"
        else:
            kind = "router"
        self.calls.append(kind)
        return ModelReply(text=self.replies[kind])


def run_turn(app, text: str, *, max_turns: int = 6):
    answers = None
    outcome = None
    for _ in range(max_turns):
        outcome = app.session.handle_turn(text, answers)
        if outcome.status != "needs_input":
            return outcome
        q = outcome.question
        answers = {q.slot: POOL.get(q.slot, (q.options[0] if q.options else "默认"))}
    return outcome


ROUTER_EXECUTE = '{"type":"execute","confidence":0.92,"reason":"读文件"}'
ROUTER_ADVISE = '{"type":"advise","confidence":0.9,"reason":"价值判断"}'
PLANNER_READ = (
    '{"steps":[{"id":"read_doc","tool":"fs.read","args":{"path":"doc.md"},'
    '"depends_on":[],"verify":{"expr":"len(content) > 0","needs":["content"]},'
    '"on_fail":"repair","gated":false}]}'
)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "doc.md").write_text("hello\n", encoding="utf-8")
    return tmp_path


def test_llm_planner_end_to_end_completes(root):
    model = DispatchModel(router=ROUTER_EXECUTE, planner=PLANNER_READ)
    app = build_app(
        root, model=model, judge=FakeJudge(),
        budget=Budget(auto_approve_tools=("shell.exec",)),
    )
    out = run_turn(app, "读一下 doc.md")
    assert out.result is not None
    assert out.result.verdict.value == "completed"
    assert [s.id for s in out.result.steps] == ["read_doc"]
    assert out.result.steps[0].checks[0]["passed"] is True
    # 一次路由 + 一次规划 + 一次收尾总结（D-042）：run 层写过总结后，会话层不再
    # 走 `_answer_from_evidence` 写第二遍（一次 run 一段叙事、不付双倍 token）。
    assert model.calls.count("planner") == 1
    assert model.calls.count("router") == 1
    assert model.calls.count("reporter") == 1
    assert out.result.summary == "已完成读取。", "收尾总结替换模板摘要"
    app.close()


PLANNER_ADVICE = (
    '{"mode":"advice","advice":{"info":["你给了期限与风险偏好"],'
    '"risks":["市场风险","信息风险","流动性风险"],'
    '"framework":["先写清目标","再写清约束","比较两种方案"],"sources":[],'
    '"disclaimer":"以上不构成投资建议。","body":"本轮只给通用框架，不给具体标的结论。"}}'
)


def test_llm_planner_advice_mode_delivers_gated_advice(root):
    # 建议由 planner 的 advice 草稿产出，过 AdviceGate 后投递（建议没有第二套生产路径）。
    model = DispatchModel(router=ROUTER_ADVISE, planner=PLANNER_ADVICE)
    # 过闸的建议用例：judge 必须存在；这里显式选 UNDECIDABLE（放行但标记），
    # 真正的 REJECT 路径由 test_llm_advice_judge_reject_falls_back 覆盖。
    app = build_app(root, model=model, judge=FakeJudge(JudgeVerdict.UNDECIDABLE))
    out = run_turn(app, "这支股票值得买吗", max_turns=8)
    assert out.status == "answered"
    delivered = [e for e in app.store.read() if e.type.value == "advice.delivered"]
    assert delivered and delivered[-1].payload["mode"] == "gated"
    assert model.calls.count("planner") == 1
    app.close()


def test_llm_advice_judge_reject_falls_back(root):
    """C1 硬约束：迁移后的 build_app 测试必须真的走一次 judge REJECT 路径。"""
    judge = FakeJudge(JudgeVerdict.REJECT)
    model = DispatchModel(router=ROUTER_ADVISE, planner=PLANNER_ADVICE)
    app = build_app(root, model=model, judge=judge)
    out = run_turn(app, "这支股票值得买吗", max_turns=8)
    try:
        assert out.status == "answered"
        assert judge.calls, "judge 必须被调用过"
        delivered = [e for e in app.store.read() if e.type.value == "advice.delivered"][-1]
        assert delivered.payload["mode"] == "fallback"
        assert delivered.payload["fallback"] == {"bool": True, "reason": "policy_reject"}
        checked = [e for e in app.store.read() if e.type.value == "gate.checked"][-1]
        assert checked.payload["judge"]["verdict"] == "reject"
        assert any(v["code"] == "policy_reject" for v in checked.payload["violations"])
    finally:
        app.close()


class OnceClarifyModel:
    """两段式桩：planner 第一次返回 clarify，之后返回 steps；router 固定 execute。

    用系统提示词里的「计划器」字样区分组件，不依赖 prompt 正文里的 JSON 键。
    """

    def __init__(self, clarify: str, steps: str) -> None:
        self._clarify = clarify
        self._steps = steps
        self.calls: list[str] = []

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
        from jagent.ports import ModelReply

        # 收尾总结（D-042）单列一类：它也是模型调用，但不是规划/路由。
        if "收尾总结" in system:
            self.calls.append("reporter")
            return ModelReply(text="读完了 doc.md。")
        if "计划器" in system:
            self.calls.append("planner")
            text = self._clarify if self.calls.count("planner") == 1 else self._steps
        else:
            self.calls.append("router")
            text = ROUTER_EXECUTE
        return ModelReply(text=text)


def test_llm_planner_clarify_mode_asks_model_questions(root):
    # planner 判定缺信息：问题由模型按 args schema 生成，不走静态槽位表；
    # 用户按动态槽位回答后，答案进槽位账本，planner 第二次起草直接出计划。
    model = OnceClarifyModel(
        clarify=(
            '{"mode":"clarify","questions":[{"slot":"target_path",'
            '"text":"要读哪个文件？","options":["doc.md","readme.md"]}]}'
        ),
        steps=PLANNER_READ,
    )
    app = build_app(
        root, model=model, judge=FakeJudge(),
        budget=Budget(auto_approve_tools=("shell.exec",)),
    )
    out = app.session.handle_turn("帮我看下那个文件")
    assert out.status == "needs_input"
    assert out.question is not None and out.question.slot == "target_path"
    asked = [e for e in app.store.read() if e.type.value == "clarify.asked"]
    assert asked and asked[-1].payload["kind"] == "planner_clarify"

    out2 = app.session.handle_turn("doc.md", {"target_path": "doc.md"})
    assert out2.status == "answered"
    assert out2.result is not None and out2.result.verdict.value == "completed"
    updated = [e for e in app.store.read() if e.type.value == "slot.updated"]
    assert any(e.payload["name"] == "target_path" for e in updated)
    assert model.calls == ["router", "planner", "router", "planner", "reporter"], model.calls
    app.close()


def test_llm_planner_reply_mode_answers_without_run(root):
    # 闲聊走 reply 模式：不建 run、不做计划，直接回答。
    model = DispatchModel(router=ROUTER_EXECUTE, planner='{"mode":"reply","text":"你好，我是 JAgent。"}')
    app = build_app(root, model=model, judge=FakeJudge())
    out = app.session.handle_turn("你好")
    assert out.status == "answered"
    assert out.result is None
    assert "JAgent" in out.text
    assert not [e for e in app.store.read() if e.type.value == "run.started"]
    delivered = [e for e in app.store.read() if e.type.value == "advice.delivered"]
    assert delivered and delivered[-1].payload["mode"] == "reply"
    app.close()


def test_llm_planner_reply_mode_directive_is_blocked(root):
    # reply 里出现指令性表述：合规扫描拦截，落兜底文本，不让风险绕过闸门。
    model = DispatchModel(
        router=ROUTER_EXECUTE,
        planner='{"mode":"reply","text":"建议你买入这只股票，稳赚。"}',
    )
    app = build_app(root, model=model, judge=FakeJudge())
    out = app.session.handle_turn("你好")
    assert out.status == "answered"
    delivered = [e for e in app.store.read() if e.type.value == "advice.delivered"]
    assert delivered and delivered[-1].payload["mode"] == "reply_fallback"
    app.close()


def test_bad_planner_output_gives_human_message_not_crash(root):
    model = DispatchModel(
        router=ROUTER_EXECUTE,
        planner='{"steps":[]}',   # 空计划，必被拒
    )
    app = build_app(root, model=model, judge=FakeJudge())
    out = run_turn(app, "读一下 doc.md")
    assert out.status == "answered"
    assert out.result is None                    # 没建 run
    assert "计划" in out.text or "换个说法" in out.text
    assert not [e for e in app.store.read() if e.type.value == "run.started"]
    app.close()


def test_no_model_asks_then_admits_it_cannot_decompose(root):
    app = build_app(root, model=NoModel())
    out = app.session.handle_turn("读一下 doc.md")
    # Router 不可用 → AskRouter 兜底：不猜意图，先问「建议还是执行」
    assert out.status == "needs_input"
    assert out.question is not None and out.question.slot == PATH_SLOT

    out2 = app.session.handle_turn("读一下 doc.md", {PATH_SLOT: "要执行"})
    assert out2.status == "answered"
    assert out2.result is None
    assert "拆" in out2.text            # 诚实承认这次拆不了，不给模板话术
    assert not [e for e in app.store.read() if e.type.value == "run.started"]
    app.close()


def test_model_quota_failure_degrades_to_honest_message(root):
    """模型层故障（403 配额/网络）不能穿透成 500，也不能假装拆过计划。"""
    from jagent.agent.model import ModelCallFailed

    class QuotaModel:
        def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
            raise ModelCallFailed('model endpoint returned an error (HTTP 403): insufficient_quota')

    app = build_app(root, model=QuotaModel(), judge=FakeJudge())
    out = app.session.handle_turn("读一下 doc.md")
    assert out.status == "answered"
    assert "模型" in out.text and "稍后再试" in out.text
    assert out.result is None
    assert out.error_code == "model_unavailable"
    assert not [e for e in app.store.read() if e.type.value == "run.started"]
    # 模型不可用不是草稿拒稿（D-012）：没有 plan.rejected，turn 出口是独立 outcome。
    rejected = [e for e in app.store.read() if e.type.value == "plan.rejected"]
    assert rejected == []
    ended = [e for e in app.store.read() if e.type.value == "turn.ended"][-1]
    assert ended.payload["outcome"] == "model_unavailable"
    app.close()


def test_llm_planner_rejects_undeclared_observation(root):
    # 断言要读 "entries"，但 fs.read 只产 content：必须被拒，不许冻结
    model = DispatchModel(
        router=ROUTER_EXECUTE,
        planner=(
            '{"steps":[{"id":"x","tool":"fs.read","args":{"path":"doc.md"},'
            '"verify":{"expr":"len(entries) > 0","needs":["entries"]}}]}'
        ),
    )
    app = build_app(root, model=model, judge=FakeJudge())
    out = run_turn(app, "读一下 doc.md")
    assert out.result is None
    assert not [e for e in app.store.read() if e.type.value == "plan.frozen"]
    app.close()
