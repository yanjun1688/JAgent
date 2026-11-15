"""v8 · 意图侧（Turn 层 + 合规校验）测试。

覆盖：AskRouter 的确定性兜底（不猜意图）、gate 的 profile / ruleset / 模式推导、
通用投递（render / fallback）。

注意：v8.2 起建议闸门只做四条对账（T1），"建议买入 / 稳赚"这类语义判断
交 T2 judge（见 tests/v8_gate 与 gate/policy.md）；本文件不再断言已删除的
v8.1 规则（advice.directive / unsourced_claim / no_risk / unaligned_target）。
"""

from __future__ import annotations

from jagent.agent.delivery import DEFAULT_DISCLAIMER, fallback, render
from jagent.agent.router import AskRouter
from jagent.contracts import Advice, IntentType, Violation
from jagent.gate.rules import AdviceGate


def _advice(**overrides: object) -> Advice:
    base = {
        "info": ("标的：沪深300ETF",),
        "risks": ("市场风险：价格波动。", "信息风险：来源有限。", "流动性风险：成交可能不及时。"),
        "framework": ("先写清目标与约束。",),
        "sources": ("https://example.com/report",),
        "disclaimer": "以上内容不构成投资建议。",
        "body": "下面只列判断要点，不给具体动作指令。",
    }
    base.update(overrides)
    return Advice(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# AskRouter：无模型时的确定性兜底，不猜意图
# --------------------------------------------------------------------------- #

def test_ask_router_never_guesses_intent() -> None:
    router = AskRouter()
    a = router.route(text="这只标的值得买吗", slots={}, turns=[])
    b = router.route(text="帮我检查一下 test 目录", slots={}, turns=[])
    c = router.route(text="今天天气不错", slots={}, turns=[])

    assert a.type is IntentType.EXECUTE
    assert a.confidence == 0.55
    assert a.needs_path_choice is True
    assert a.missing_params == ()
    # 同一套输入形态永远得到同一个决策（可复现，正是确定性兜底的职责）
    assert (a.type, a.confidence) == (b.type, b.confidence) == (c.type, c.confidence)
    assert a.reason == b.reason == c.reason


# --------------------------------------------------------------------------- #
# gate：profile / ruleset / framework 模式（T1 的规则见 tests/v8_gate）
# --------------------------------------------------------------------------- #

def test_gate_without_evidence_is_framework_and_passes() -> None:
    verdict = AdviceGate().check(_advice())

    assert verdict.passed is True
    assert verdict.violations == ()
    assert verdict.profile == "advice"
    assert verdict.ruleset == "advice.v8.2"
    assert verdict.mode == "framework"
    assert verdict.fallback is None


def test_framework_only_flag_is_still_accepted_for_session_compatibility() -> None:
    """framework_only 只是 Session 旧调用形状的兼容参数（临时，等 D 接证据）。"""

    verdict = AdviceGate().check(_advice(body="这只股票的走势偏强。"), framework_only=True)

    assert verdict.passed is True
    assert verdict.mode == "framework"


# --------------------------------------------------------------------------- #
# delivery / fallback
# --------------------------------------------------------------------------- #

def test_fallback_cites_violation_codes_and_stays_compliant() -> None:
    advice = fallback((Violation(code="ADVICE_NO_CITATION", rule_id="advice.no_citation"),))
    text = render(advice)

    assert "ADVICE_NO_CITATION" in text
    assert "不会作为结论发给你" in text
    assert "【决策框架】" in text
    assert "【风险提示】" in text
    assert "【声明】" in text
    assert advice.sources == ()
    assert DEFAULT_DISCLAIMER == advice.disclaimer
    # 兜底文本构造上必须能过闸门（它是"零模型、肯定合规"的最后一条路径）
    assert AdviceGate().check(advice).passed


def test_fallback_without_violations_says_no_basis() -> None:
    advice = fallback(())
    text = render(advice)

    assert "可核对的依据" in text
    assert AdviceGate().check(advice).passed


def test_render_is_multi_paragraph_for_regular_advice() -> None:
    text = render(_advice())

    assert "【关键信息】" in text
    assert "【决策框架】" in text
    assert "【风险提示】" in text
    assert "【信息来源】" in text
    assert "https://example.com/report" in text
    assert "不构成投资建议" in text
