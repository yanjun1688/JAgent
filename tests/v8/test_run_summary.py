"""收尾总结接线（D-042）：run 到终态后用模型写一段人话，替换模板摘要。

用户实测反馈（2026-11-15）：任务跑完对话区只有"1/1 步通过"，没有"这件事到底做出了
什么结果"。修法是把 Reporter 的模板摘要换成模型起草的复述——但**判定不许动**：

- ``verdict`` / ``steps`` / ``remaining_plan`` / ``budget_used`` 仍然来自受信组件的
  Result（I3：模型只起草，代码判定）；
- 模型写的总结**只许复述事实包里的东西**；``generated_by`` 记 ``template+model``，
  报告产物与 ``report.rendered`` 里落的就是同一句；
- 模型不可用 / 调用坏了 / 输出为空 → 退回模板摘要，warn 一行，run 照常落终态；
- 没注入总结器（离线装配 / 替身装配）→ 与今天完全一样。
"""

from __future__ import annotations

import logging

import pytest

from jagent.app.runner import Runner
from jagent.contracts import Budget, EventType, Plan, RunVerdict, Step, Verify
from jagent.harness.repair import HeuristicRepairer
from jagent.harness.report import Reporter
from jagent.harness.verify import Verifier
from tests.v8.conftest import Rig


class RecordingSummarizer:
    """记录 runner 交给它的事实包，回一段固定文本。"""

    def __init__(self, text: str = "读取了 a.txt，内容已交给后续步骤。") -> None:
        self._text = text
        self.facts: list[dict] = []

    def summarize(self, facts: dict) -> str:
        self.facts.append(facts)
        return self._text


class BrokenSummarizer:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def summarize(self, facts: dict) -> str:
        raise self._exc


def _runner(rig: Rig, summarizer) -> Runner:
    return Runner(
        writer=rig.writer,
        executor=rig.executor,
        verifier=Verifier(),
        repairer=HeuristicRepairer(),
        reporter=Reporter(),
        human=rig.human,
        budget=Budget(),
        registry=rig.registry,
        summarizer=summarizer,
    )


def _read_plan(*, verify: str = "ok == true") -> Plan:
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


def _prepare(rig: Rig) -> None:
    (rig.ws / "a.txt").write_text("hello", encoding="utf-8")


def test_completed_run_summary_is_the_draft(rig: Rig) -> None:
    _prepare(rig)
    summarizer = RecordingSummarizer()

    outcome = _runner(rig, summarizer).start_and_run(_read_plan())

    assert outcome.status == "completed", outcome.reason
    assert outcome.result is not None
    assert outcome.result.summary == "读取了 a.txt，内容已交给后续步骤。"
    assert outcome.result.generated_by == "template+model", "来源要能看出有模型参与"
    rendered = rig.of_type(EventType.REPORT_RENDERED)[-1].payload["result"]
    assert rendered["summary"] == outcome.result.summary, "报告产物与返回必须同一句"
    assert rendered["generated_by"] == "template+model"


def test_summary_does_not_change_the_verdict_or_the_steps(rig: Rig) -> None:
    """I3：总结是文本复述，不是判定——判定字段逐项等于受信组件的产出。"""

    _prepare(rig)
    outcome = _runner(rig, RecordingSummarizer()).start_and_run(_read_plan())

    assert outcome.result is not None
    assert outcome.result.verdict is RunVerdict.COMPLETED
    assert [step.id for step in outcome.result.steps] == ["s1"]
    assert [step.status for step in outcome.result.steps] == ["passed"]
    assert outcome.result.remaining_plan == ()
    assert rig.world.runs[outcome.run_id].status == "completed"


def test_drafter_sees_the_facts_not_the_verdict_of_the_model(rig: Rig) -> None:
    _prepare(rig)
    summarizer = RecordingSummarizer()

    _runner(rig, summarizer).start_and_run(_read_plan())

    facts = summarizer.facts[0]
    assert facts["run_id"]
    assert facts["verdict"] == "completed"
    assert facts["steps_total"] == 1 and facts["steps_passed"] == 1
    step = facts["steps"][0]
    assert step["id"] == "s1" and step["tool"] == "fs.read" and step["status"] == "passed"
    assert "hello" in str(step["observations"]), "关键结果是观测里的事实，不是模型记忆"


def test_failed_run_still_gets_a_draft_with_the_failure_facts(rig: Rig) -> None:
    _prepare(rig)
    summarizer = RecordingSummarizer("读取成功，但断言没通过。")

    outcome = _runner(rig, summarizer).start_and_run(_read_plan(verify="content == 'other'"))

    assert outcome.status == "failed"
    assert outcome.result is not None
    assert outcome.result.summary == "读取成功，但断言没通过。"
    facts = summarizer.facts[0]
    assert facts["verdict"] == "failed"
    assert facts["steps"][0]["status"] == "failed"
    assert facts["steps"][0]["failure_class"], "失败分类必须来自受信组件"
    assert facts["fail_reason"], "失败原因必须进事实包"
    assert outcome.result.verdict is RunVerdict.FAILED


def test_broken_drafter_falls_back_to_template_summary(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    _prepare(rig)
    caplog.set_level(logging.INFO, logger="jagent.chain")

    with caplog.at_level(logging.WARNING, logger="jagent.chain"):
        outcome = _runner(rig, BrokenSummarizer(RuntimeError("模型挂了"))).start_and_run(_read_plan())

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.summary == "1/1 步通过", "拿不到总结就退回模板，不编"
    assert outcome.result.generated_by == "template"
    assert any("summary fallback" in record.message for record in caplog.records)


def test_blank_draft_falls_back_to_template_summary(rig: Rig) -> None:
    _prepare(rig)

    outcome = _runner(rig, RecordingSummarizer("   ")).start_and_run(_read_plan())

    assert outcome.result is not None
    assert outcome.result.summary == "1/1 步通过"
    assert outcome.result.generated_by == "template"


def test_no_drafter_keeps_the_template_path_untouched(rig: Rig) -> None:
    _prepare(rig)

    outcome = _runner(rig, None).start_and_run(_read_plan())

    assert outcome.result is not None
    assert outcome.result.summary == "1/1 步通过"
    assert outcome.result.generated_by == "template"


def test_draft_with_a_directive_is_rejected_by_the_reply_gate(rig: Rig) -> None:
    """模型写的总结也是对外文本：指令性表述必须被 reply 闸拦下，退回模板。"""

    _prepare(rig)

    outcome = _runner(rig, RecordingSummarizer("别等了，赶紧买入，稳赚不赔。")).start_and_run(_read_plan())

    assert outcome.result is not None
    assert outcome.result.summary == "1/1 步通过", "被闸门拦下的草稿不能投递"
    assert outcome.result.generated_by == "template"


def test_repair_budget_note_is_visible_to_the_drafter(rig: Rig) -> None:
    """``_fail`` 里补的"转人工"说明不能被总结吞掉：它以 note 进事实包。"""

    _prepare(rig)
    summarizer = RecordingSummarizer("断言失败，已转人工。")

    runner = _runner(rig, summarizer)
    runner.budget = Budget(max_steps=5, max_repairs=0)
    outcome = runner.start_and_run(_read_plan(verify="content == 'other'"))

    assert outcome.status == "failed"
    facts = summarizer.facts[0]
    assert "修复预算" in facts["note"] or "转人工" in facts["note"]


# --------------------------------------------------------------------------- #
# 会话层：一次 run 只写一段总结（D-042）
# --------------------------------------------------------------------------- #

_PLAN_JSON = (
    '{"steps":[{"id":"s1","tool":"fs.read","args":{"path":"a.txt"},'
    '"depends_on":[],"verify":{"expr":"len(content) > 0","needs":["content"]},'
    '"on_fail":"repair","gated":false}]}'
)


class _RoutePlanReportModel:
    """三路桩：router 固定 execute；planner 回计划；收尾总结回人话。

    按系统提示词的锚点分派（与 e2e 的 PurposeModel 同思路），不依赖 prompt 正文。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
        from jagent.ports import ModelReply

        if "收尾总结" in system:
            self.calls.append("reporter")
            return ModelReply(text="读到了 a.txt，内容是 hello。")
        if "计划器" in system:
            self.calls.append("planner")
            return ModelReply(text=_PLAN_JSON)
        self.calls.append("router")
        return ModelReply(text='{"type":"execute","confidence":0.92,"reason":"读文件"}')


class _NoOpinionJudge:
    model = "stub-judge"

    def judge(self, request: object) -> None:
        return None


def test_model_summary_is_written_once_per_run(tmp_path) -> None:
    """run 层写过总结后，会话层不再写第二遍：一段叙事、一次调用。"""

    from jagent.wiring import build_app

    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.txt").write_text("hello", encoding="utf-8")
    model = _RoutePlanReportModel()
    app = build_app(tmp_path, model=model, judge=_NoOpinionJudge())
    try:
        out = app.session.handle_turn("读一下 a.txt")
        events = app.store.read()
    finally:
        app.close()

    assert out.status == "answered", out.text
    assert out.result is not None
    assert out.result.summary == "读到了 a.txt，内容是 hello。"
    assert out.text == out.result.summary, "会话回复就是同一段总结（前端 message 同源）"
    assert model.calls.count("reporter") == 1
    assert model.calls.count("planner") == 1, "会话层不该再走一遍证据收尾"
    ended = [e for e in events if e.type is EventType.TURN_ENDED]
    assert ended[-1].payload["outcome"] == "answered", "交付了模型写的回答就不是 handed_off"
