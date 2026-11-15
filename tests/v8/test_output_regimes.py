"""输出分级（Output Regimes）· 契约测试（先红后绿）。

对应 `docs/jagent-v8-output-regimes.md` 与宪法 v2 的 I3 / I8 / I9。这些断言在改造前
**必须红**——它们钉的正是今天的结构缺陷：

- I8：终态记录（`report.rendered` / `run.completed`）只能装 verified 事实（模板可重算），
  模型草稿不得进 `Result`；
- I8：模型调用不得发生在 run 的终态事务里（否则 `wall_s`/超时语义失真、终态被慢模型拖住）；
- I3：对外文本必须带 `regime`，且 `grounded` 必须列出它过了哪些确定性检查；
- I3：没有判据的输出只能是 `unverified`，不许出现"通过"；
- 污点：delivery 文本不进入证据包。
"""

from __future__ import annotations

import json

from jagent.api.app import create_app
from jagent.contracts import Budget
from jagent.harness.report import Reporter
from jagent.wiring import build_app

PLAN_READ_PRICE = json.dumps(
    {
        "steps": [
            {
                "id": "s1",
                "tool": "fs.read",
                "args": {"path": "quote.txt"},
                "depends_on": [],
                "verify": {"expr": "len(content) > 0", "needs": ["content"]},
                "on_fail": "repair",
                "gated": False,
            }
        ]
    },
    ensure_ascii=False,
)

SUMMARY = "读取完成：文件里的最新价是 17.19 元。"


class _Model:
    """router / planner / reporter / reply 四路桩，按系统提示词的锚点分派。"""

    def __init__(self, *, reply: str = "", summary: str = SUMMARY) -> None:
        self.reply = reply
        self.summary = summary
        self.calls: list[str] = []

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
        from jagent.ports import ModelReply

        if "收尾总结" in system:
            self.calls.append("reporter")
            return ModelReply(text=self.summary)
        if "计划器" in system:
            self.calls.append("planner")
            if self.reply:
                return ModelReply(text=json.dumps({"mode": "reply", "text": self.reply},
                                                  ensure_ascii=False))
            return ModelReply(text=PLAN_READ_PRICE)
        self.calls.append("router")
        kind = "advise" if self.reply else "execute"
        return ModelReply(text=json.dumps({"type": kind, "confidence": 0.9, "reason": "stub"}))


class _Judge:
    model = "stub-judge"

    def judge(self, request: object) -> None:
        return None


def _app(tmp_path, model: _Model):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "quote.txt").write_text("last_price=17.19\n", encoding="utf-8")
    return build_app(
        tmp_path, model=model, judge=_Judge(), budget=Budget(auto_approve_tools=("shell.exec",))
    )


def _delivered(app) -> list[dict]:
    return [e.payload for e in app.store.read() if e.type.value == "advice.delivered"]


# --------------------------------------------------------------------------- #
# I8：终态记录只装 verified 事实
# --------------------------------------------------------------------------- #

def test_terminal_record_is_the_pure_template_not_the_model_text(tmp_path) -> None:
    app = _app(tmp_path, _Model())
    try:
        outcome = app.session.handle_turn("读一下行情文件")
        rendered = [e for e in app.store.read() if e.type.value == "report.rendered"][-1]
        pure = Reporter().render(app.world.runs[outcome.run_id]).summary
    finally:
        app.close()

    assert rendered.payload["result"]["summary"] == pure, (
        "终态记录里出现了模型文本：report.rendered 必须能由 Reporter 纯函数重算（I8）"
    )
    assert "17.19" not in rendered.payload["result"]["summary"], "模型措辞不得进终态事件"


def test_summary_is_drafted_after_the_run_is_terminal(tmp_path) -> None:
    model = _Model()
    app = _app(tmp_path, model)
    try:
        app.session.handle_turn("读一下行情文件")
        types = [e.type.value for e in app.store.read()]
        kinds = [e.payload.get("purpose") for e in app.store.read()
                 if e.type.value == "model.called"]
    finally:
        app.close()

    assert "run.completed" in types
    terminal = types.index("run.completed")
    # 收尾总结那次调用必须排在终态之后：模型不进 run 的终态事务（I8）
    reporter_at = next(
        i for i, kind in enumerate(kinds) if kind == "reporter"
    )
    called = [i for i, t in enumerate(types) if t == "model.called"]
    assert called[reporter_at] > terminal, (
        "收尾总结的 model.called 出现在 run.completed 之前：模型拖住了终态事务"
    )


# --------------------------------------------------------------------------- #
# I3：三值 + 检查记录
# --------------------------------------------------------------------------- #

def test_regime_enum_has_three_values_and_no_pass() -> None:
    from jagent.contracts import Regime

    assert {member.value for member in Regime} == {"verified", "grounded", "unverified"}
    assert not hasattr(Regime, "PASS"), "regime 里不许出现'通过'：没有判据就没有 pass（I3）"


def test_completed_run_delivery_is_grounded_with_checks(tmp_path) -> None:
    app = _app(tmp_path, _Model())
    try:
        app.session.handle_turn("读一下行情文件")
        outcome = app.session.state  # keep ref alive for nothing; payload asserted below
        del outcome
        delivered = _delivered(app)
    finally:
        app.close()

    assert delivered, "run 结束后没有投递事件"
    payload = delivered[-1]
    delivery = payload.get("delivery")
    assert delivery, "advice.delivered 缺 delivery 字段（regime/checks/grounds）"
    assert delivery["regime"] == "grounded"
    assert "numbers.grounded" in delivery["checks"], "数字对账必须记录为检查项"
    assert delivery["grounds"], "grounded 文本必须能指回事件 seq / 产物"
    assert payload["mode"] == "report"


def test_freeform_reply_is_marked_unverified(tmp_path) -> None:
    model = _Model(reply="这个问题可以从三个角度看看：目标、约束、可逆性。")
    app = _app(tmp_path, model)
    try:
        app.session.handle_turn("帮我理一下思路")
        delivered = _delivered(app)
    finally:
        app.close()

    delivery = delivered[-1].get("delivery") or {}
    assert delivery.get("regime") == "unverified", "没有判据的回答必须标未核验（I3）"
    assert not delivery.get("grounds"), "unverified 不该假装有来源"


def test_api_exposes_the_delivery_regime(tmp_path) -> None:
    model = _Model()
    app = _app(tmp_path, model)
    api = create_app(root=tmp_path, app=app)
    from starlette.testclient import TestClient

    try:
        with TestClient(api) as client:
            body = client.post("/chat", json={"text": "读一下行情文件"}).json()
    finally:
        app.close()

    assert body["status"] == "answered"
    assert body["delivery"]["regime"] == "grounded"
    assert body["message"] == SUMMARY, "面板气泡必须是这段复述，不是模板摘要"


# --------------------------------------------------------------------------- #
# 污点：delivery 文本不进证据包
# --------------------------------------------------------------------------- #

def test_delivery_text_never_becomes_evidence(tmp_path) -> None:
    app = _app(tmp_path, _Model())
    try:
        outcome = app.session.handle_turn("读一下行情文件")
        evidence = app.session._evidence_of(outcome.run_id)
    finally:
        app.close()

    blob = json.dumps(evidence, ensure_ascii=False)
    assert SUMMARY not in blob, "总结文本进了证据包：措辞不是事实（污点规则）"
