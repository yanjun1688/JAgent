"""普通执行的模型总结收尾不得丢 run 判定（在线链路发现的回归）。

D-004 的总结路径：普通 execute 完成后由 `_answer_from_evidence` 让模型基于
证据做收尾（reply 或 advice）；gather / `then="advice"` 则走 E19 的
`_compose_advice`。两条路径在有草稿模型时返回的 TurnOutcome 曾不带
``run_id`` / ``result``，API 的 ``body["result"]``（verdict / summary）随之消失。

离线测试只覆盖无模型的 `_handed_off` 分支（DraftPlanner 用尽后重复最后的
Plan，总结调用返回 Plan 而非 ReplyDraft），e2e_live 长期 deselected——两个
门禁都没拦住，直到真实模型在线冒烟把 ``verdict=none`` 炸出来。
"""

from __future__ import annotations

from jagent.contracts import (
    Advice,
    Citation,
    EvidenceReq,
    IntentType,
    Plan,
    ReplyDraft,
    Step,
    Verify,
)
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter

from .support import FakeJudge, FakePropose, write_doc


def _read() -> Step:
    return Step(
        id="read",
        tool="fs.read",
        args={"path": "doc.md"},
        required=True,
        verify=Verify("len(content) > 0", needs=("content",)),
    )


def test_reply_summary_keeps_run_verdict(tmp_path) -> None:
    """run 完成后走 reply 总结：TurnOutcome 必须保留 run_id 与 verdict。"""

    plan = Plan(steps=(_read(),), then=None)
    planner = DraftPlanner([plan, ReplyDraft(text="文件读取完成，内容已确认存在。")])
    app = build_app(
        tmp_path,
        planner=planner,
        router=FixedRouter(IntentType.EXECUTE),
        judge=FakeJudge(),
    )
    write_doc(app)
    try:
        out = app.session.handle_turn("读一下 doc.md")
    finally:
        app.close()

    assert out.status == "answered"
    assert out.run_id is not None, "总结收尾丢了 run_id"
    assert out.result is not None, "总结收尾丢了 run 判定：API 的 body['result'] 会消失"
    assert out.result.verdict.value == "completed"


def test_e19_compose_keeps_run_verdict(tmp_path) -> None:
    """gather 合成收尾同样必须保留 run_id 与 verdict（同一条回归的另一条路径）。"""

    plan = Plan(
        steps=(_read(),),
        then="advice",
        evidence_requires=(EvidenceReq("read", "content"),),
    )
    advice = Advice(body="文件存在，内容非空。", refs=(Citation("read", "content"),))
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.GATHER),
        propose=FakePropose(advice),
        judge=FakeJudge(),
    )
    write_doc(app)
    try:
        out = app.session.handle_turn("先看文件再判断")
    finally:
        app.close()

    assert out.status == "answered"
    assert out.run_id is not None, "合成收尾丢了 run_id"
    assert out.result is not None, "合成收尾丢了 run 判定：API 的 body['result'] 会消失"
    assert out.result.verdict.value == "completed"
