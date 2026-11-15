"""复现（bug）：带真实观测数字的收尾总结，被 reply 合规扫描判成"无来源数字声明"，
用户看到的又是机械摘要"1/1 步通过"。

现场（2026-11-15，qwen3.8-27b，run_c1dac42fcd2c，兖矿能源 600188.SH）：

    model.call purpose=reporter ... reply_chars=516
    run=run_c1dac42fcd2c summary rejected codes=["REPLY_UNSOURCED_CLAIM"]
    run=run_c1dac42fcd2c run COMPLETED summary=1/1 步通过

根因不是模型、也不是接线：`scan_reply` 是**闲聊 reply** 的扫描器（没有证据包，一切
"元/%/年"都算无来源）；用它去判一段"复述本次观测"的文本，必然把 17.19 元判成捏造。

这组测试只钉**用户可见行为**（不预设实现）：观测里的真实数字必须能出现在回答里；
事实包里没有的数字必须进不来。
"""

from __future__ import annotations

import json

from jagent.api.app import _message
from jagent.contracts import Budget
from jagent.wiring import build_app

#: 观测里的真实价格（工具返回 / 文件内容都用它）。
PRICE = "17.19"

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


class _Model:
    """三路桩：router 固定 execute、planner 回计划、收尾总结回一段带价格的复述。"""

    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls: list[str] = []

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
        from jagent.ports import ModelReply

        if "收尾总结" in system:
            self.calls.append("reporter")
            return ModelReply(text=self.summary)
        if "计划器" in system:
            self.calls.append("planner")
            return ModelReply(text=PLAN_READ_PRICE)
        self.calls.append("router")
        return ModelReply(text='{"type":"execute","confidence":0.92,"reason":"读行情文件"}')


class _Judge:
    model = "stub-judge"

    def judge(self, request: object) -> None:
        return None


def _plan_app(tmp_path, model: _Model):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "quote.txt").write_text(f"last_price={PRICE}\n", encoding="utf-8")
    return build_app(
        tmp_path, model=model, judge=_Judge(), budget=Budget(auto_approve_tools=("shell.exec",))
    )


def test_grounded_price_in_summary_reaches_the_user(tmp_path) -> None:
    """观测里有 17.19，总结复述 17.19 → 用户必须看到这句话，而不是"1/1 步通过"。"""

    summary = f"读取完成：行情文件里的最新价是 {PRICE} 元。"
    model = _Model(summary)
    app = _plan_app(tmp_path, model)
    try:
        outcome = app.session.handle_turn("读一下行情文件")
    finally:
        app.close()

    assert outcome.status == "answered", outcome.text
    assert PRICE in outcome.text, f"回答里丢了观测里的价格：{outcome.text!r}"
    assert outcome.text == summary
    assert _message(outcome) == summary, "API 给面板的 message 也必须是这句（不是模板摘要）"


def test_summary_with_a_fabricated_number_is_dropped(tmp_path) -> None:
    """事实包里没有 99.9%，模型编的 → 不许投递（降级回可核文本）。"""

    model = _Model(f"读取完成：文件里的最新价是 {PRICE} 元，准确率 99.9%。")
    app = _plan_app(tmp_path, model)
    try:
        outcome = app.session.handle_turn("读一下行情文件")
    finally:
        app.close()

    assert "99.9" not in outcome.text, f"编造的数字漏进了回答：{outcome.text!r}"
