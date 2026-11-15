"""缺口 5 · 投递全文落产物库（STATUS 缺口表 #5）。

事件里 advice / report 只有结构化摘要，全文散落在对话流里——事后想按
artifact_id 取回「当时到底投了什么」做不到。修复：
- ``advice.delivered`` / ``report.rendered`` 载荷增加 ``artifact_id``；
- 全文（投递文本 / 报告摘要）进 ArtifactStore（内容寻址，重放可取回）；
- Session / Runner 未注入 artifacts 时跳过（直接构造的测试不受影响）。
"""

from __future__ import annotations

from jagent.contracts import (
    Budget,
    EventType,
    IntentType,
    Plan,
    Step,
    Verify,
)
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter


def test_advice_and_report_text_land_in_artifacts(tmp_path) -> None:
    """Given 一个正常完成的写文件 run（收尾投递），When 读事件与产物库，
    Then 投递文本与报告文本都带 artifact_id，且全文可从产物库按 id 取回。

    D-042 之后：``report.rendered`` 是模板事实（RunRecord），``advice.delivered{mode:report}``
    是给用户看的那段文本（Delivery）；两者各落一份产物，内容寻址。
    """
    plan = Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "done.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
            ),
        ),
    )
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.EXECUTE),
        budget=Budget(auto_approve_tools=("fs.write",)),
    )
    try:
        out = app.session.handle_turn("写 done.txt")
        assert out.status == "answered"

        events = app.store.read()
        delivered = [e.payload for e in events if e.type is EventType.ADVICE_DELIVERED][-1]
        report = [e.payload for e in events if e.type is EventType.REPORT_RENDERED][-1]

        assert delivered.get("artifact_id"), "投递全文没落产物库"
        assert report.get("artifact_id"), "报告全文没落产物库"

        delivery_text = app.artifacts.get_text(delivered["artifact_id"])
        assert delivery_text, "投递全文取回为空"
        assert delivered["delivery"]["regime"] == "verified", "没有模型装配时是模板文本"
        assert app.artifacts.get_text(report["artifact_id"]), "报告全文取回为空"

        # 内容寻址：产物 id 必须是全文的 sha256（与库的完整性校验一致）。
        import hashlib

        assert delivered["artifact_id"] == hashlib.sha256(delivery_text.encode()).hexdigest()
    finally:
        app.close()
