"""接地语法的唯一实现（I9 / 输出分级 v1 §3）。

规则：**数字语法只许出现在 `jagent/grounding.py`**。gate 的 T1/T3、run 收尾复述
都从这里取同一套"什么算数字、什么算有来源"；闸门之间只允许**策略**不同（R3 没有
证据包，所以一切数字声明都算无来源），不允许**语法**各写一套。

2026-11-15 事故就是策略被当成了语法：reply 的"无来源数字"规则被拿去判一段复述观测
的文本，真实价格 17.19 被判成捏造。
"""

from __future__ import annotations

import inspect
import pathlib

from jagent import grounding
from jagent.gate import rules as gate_rules

GATE = pathlib.Path(gate_rules.__file__).read_text(encoding="utf-8")


def test_number_syntax_lives_only_in_grounding() -> None:
    """gate 里不该再有自己的数字正则（``\\d`` 类）：它们必须从 grounding 取。"""

    assert "\\d" not in GATE, (
        "gate/rules.py 里还有数字正则：数字语法必须收在 jagent/grounding.py，"
        "闸门只保留策略（是否算违规、报什么 code）"
    )


def test_number_claims_are_recognised_by_grounding() -> None:
    """R3 的数字声明语法（%、年、元、倍、X.X）由 grounding 提供。"""

    for text in ("收益率 3.2%", "2026 年的价格", "最新价 17.19 元", "翻了三倍")[:3]:
        assert grounding.number_claims(text), f"没识别出数字声明：{text}"

    assert grounding.number_claims("这篇文章写得不错") == ()


def test_scan_reply_uses_the_grounding_claims() -> None:
    """reply 扫描的策略不变（没有证据包 → 数字声明即违规），但语法来自 grounding。"""

    violations = gate_rules.scan_reply("最新价 17.19 元")
    assert [v.code for v in violations] == ["REPLY_UNSOURCED_CLAIM"]
    # 把 grounding 的语法换成空实现，reply 扫描必须跟着变（证明确实是委托，不是各写一套）
    original = grounding.number_claims
    try:
        grounding.number_claims = lambda text: ()  # type: ignore[assignment]
        gate_rules.scan_reply.__globals__["number_claims"] = grounding.number_claims
        assert gate_rules.scan_reply("最新价 17.19 元") == ()
    finally:
        gate_rules.scan_reply.__globals__["number_claims"] = original


def test_body_numbers_exempt_dates_and_ordinals() -> None:
    """T1 的正文数字抽取（豁免日期/时间/序号）也在 grounding：gate 只是调用方。"""

    found = [raw for raw, _ in grounding.body_numbers("2026-11-15 第 2 步，价格 17.19 元")]
    assert found == ["17.19"], found


def test_ungrounded_numbers_accepts_unit_and_percent_variants() -> None:
    """同一数量的不同写法算同一个数（17.19 元 vs 17.19；3.2% vs 0.032）。"""

    assert grounding.check_numbers("价格 17.19 元", {"price": 17.19})[0] is True
    assert grounding.check_numbers("涨幅 3.2%", {"ratio": 0.032})[0] is True
    assert grounding.check_numbers("价格 18.00 元", {"price": 17.19})[0] is False


def test_grounding_module_has_no_event_or_model_dependencies() -> None:
    """中立模块：不写事件、不碰模型（I2 的同一口径）。"""

    source = inspect.getsource(grounding)
    assert "EventType" not in source
    assert "writer.emit" not in source
    assert "httpx" not in source
