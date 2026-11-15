"""run 收尾投递（R2 · grounded）：终态之后生成人话，过接地检查，带 regime 投递。

改造后（D-042 / 输出分级 v1）职责分工变了，这组测试跟着变：

- **Runner 不再调模型**：`report.rendered` 里的 summary 永远是模板（纯函数可重算，I8）；
- **模型在 Session 的投递步骤里**：草稿 + `numbers.grounded` / `directives.absent` 检查；
- 不合格/不可用 → 模板文本（`verified`）+ `fallback{bool,reason}`，不阻断事实；
- 一切判定字段（verdict / steps / remaining / budget）仍逐项来自受信 Result（I3）。
"""

from __future__ import annotations

from jagent.contracts import Budget, EventType, IntentType, Plan, Step, Verify
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter


class RecordingSummarizer:
    """记录事实包，回一段可指定文本。"""

    def __init__(self, text: str = "读取了 a.txt，内容已交给后续步骤。") -> None:
        self.text = text
        self.facts: list[dict] = []

    def summarize(self, facts: dict) -> str:
        self.facts.append(facts)
        return self.text


class BrokenSummarizer:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def summarize(self, facts: dict) -> str:
        raise self.exc


def _plan(*, verify: str = "ok == true") -> Plan:
    return Plan(
        steps=(
            Step(
                id="s1",
                tool="fs.read",
                args={"path": "a.txt"},
                verify=Verify(verify, needs=("ok", "content")),
            ),
        )
    )


def _app(tmp_path, summarizer, *, plan: Plan | None = None, budget: Budget | None = None):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "a.txt").write_text("hello", encoding="utf-8")
    return build_app(
        tmp_path,
        planner=DraftPlanner([plan or _plan()]),
        router=FixedRouter(IntentType.EXECUTE),
        budget=budget,
        summarizer=summarizer,
    )


def _delivered(app) -> list[dict]:
    return [e.payload for e in app.store.read() if e.type is EventType.ADVICE_DELIVERED]


def _report(app) -> dict:
    return [e.payload for e in app.store.read() if e.type is EventType.REPORT_RENDERED][-1]


def test_completed_run_text_is_the_draft_and_artifact_is_the_template(tmp_path) -> None:
    summarizer = RecordingSummarizer()
    app = _app(tmp_path, summarizer)
    try:
        out = app.session.handle_turn("读一下 a.txt")
        report = _report(app)
        delivered = _delivered(app)[-1]
    finally:
        app.close()

    assert out.status == "answered"
    assert out.text == summarizer.text, "气泡是复述"
    assert out.result is not None and out.result.summary == "1/1 步通过", "Result 仍是模板事实"
    assert report["result"]["summary"] == "1/1 步通过", "终态记录里没有模型文本（I8）"
    assert delivered["mode"] == "report"
    assert delivered["delivery"]["regime"] == "grounded"
    assert delivered["delivery"]["checks"] == ["directives.absent", "numbers.grounded"]
    assert delivered["delivery"]["grounds"], "接地文本必须指回事件 seq"


def test_draft_does_not_change_the_verdict_or_steps(tmp_path) -> None:
    app = _app(tmp_path, RecordingSummarizer("随便写点什么，反正没有数字。"))
    try:
        out = app.session.handle_turn("读一下 a.txt")
    finally:
        app.close()

    assert out.result is not None
    assert out.result.verdict.value == "completed"
    assert [s.status for s in out.result.steps] == ["passed"]
    assert out.result.remaining_plan == ()


def test_failed_run_gets_a_draft_with_the_failure_facts(tmp_path) -> None:
    summarizer = RecordingSummarizer("读取成功，但断言没通过。")
    app = _app(tmp_path, summarizer, plan=_plan(verify="content == 'other'"))
    try:
        out = app.session.handle_turn("读一下 a.txt")
    finally:
        app.close()

    assert out.status == "answered" and out.result is not None
    assert out.result.verdict.value == "failed"
    facts = summarizer.facts[-1]
    assert facts["verdict"] == "failed"
    assert facts["steps"][0]["status"] == "failed"
    assert facts["steps"][0]["failure_class"], "失败分类必须来自受信组件"
    assert facts["fail_reason"], "失败原因必须进事实包"


def test_repair_budget_note_reaches_the_drafter(tmp_path) -> None:
    """"转人工"这类运行侧补的说明在模板摘要里，事实包必须带上它。"""

    summarizer = RecordingSummarizer("断言失败，已转人工。")
    app = _app(
        tmp_path, summarizer,
        plan=_plan(verify="content == 'other'"),
        budget=Budget(max_repairs=0),
    )
    try:
        app.session.handle_turn("读一下 a.txt")
    finally:
        app.close()

    facts = summarizer.facts[-1]
    assert "修复预算" in facts["template_summary"] or "转人工" in facts["template_summary"]


def test_broken_drafter_falls_back_to_template(tmp_path, caplog) -> None:
    import logging

    caplog.set_level(logging.WARNING, logger="jagent.chain")
    app = _app(tmp_path, BrokenSummarizer(RuntimeError("模型挂了")))
    try:
        out = app.session.handle_turn("读一下 a.txt")
        delivered = _delivered(app)[-1]
    finally:
        app.close()

    assert out.text == "1/1 步通过", "拿不到复述就退回模板，不编"
    assert out.delivery is not None and out.delivery.regime.value == "verified"
    assert delivered["delivery"]["fallback"]["reason"] == "model_unavailable"
    assert any("delivery downgraded" in record.message for record in caplog.records)


def test_blank_draft_falls_back_to_template(tmp_path) -> None:
    app = _app(tmp_path, RecordingSummarizer("   "))
    try:
        out = app.session.handle_turn("读一下 a.txt")
        delivered = _delivered(app)[-1]
    finally:
        app.close()

    assert out.text == "1/1 步通过"
    assert delivered["delivery"]["fallback"]["reason"] == "draft_empty"


def test_directive_draft_falls_back_to_template(tmp_path) -> None:
    app = _app(tmp_path, RecordingSummarizer("别等了，赶紧买入。"))
    try:
        out = app.session.handle_turn("读一下 a.txt")
        delivered = _delivered(app)[-1]
    finally:
        app.close()

    assert out.text == "1/1 步通过"
    assert delivered["delivery"]["fallback"]["reason"] == "directive"


def test_fabricated_number_falls_back_to_template(tmp_path) -> None:
    app = _app(tmp_path, RecordingSummarizer("读取完成，准确率 99.9%。"))
    try:
        out = app.session.handle_turn("读一下 a.txt")
        delivered = _delivered(app)[-1]
    finally:
        app.close()

    assert out.text == "1/1 步通过"
    assert delivered["delivery"]["fallback"]["reason"] == "number_ungrounded"


def test_no_drafter_keeps_the_template_text(tmp_path) -> None:
    app = _app(tmp_path, None)
    try:
        out = app.session.handle_turn("读一下 a.txt")
        delivered = _delivered(app)[-1]
    finally:
        app.close()

    assert out.text == "1/1 步通过"
    assert delivered["delivery"]["regime"] == "verified"


def test_one_delivery_per_run(tmp_path) -> None:
    """一次 run 只有一条投递（禁止两套叙事，宪法 §4.5）。"""

    app = _app(tmp_path, RecordingSummarizer("读到了 hello。"))
    try:
        app.session.handle_turn("读一下 a.txt")
        delivered = _delivered(app)
    finally:
        app.close()

    assert [p["mode"] for p in delivered] == ["report"]
