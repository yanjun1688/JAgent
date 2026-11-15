"""Wave 2 · C3 · 路由三类（grounding §1 / D-011 / D-016）。

这里钉住三件事：

* 三类各有一条判据，模型给出的 ``gather`` / ``execute`` / ``advise`` 都被采信；
* 枚举外的值仍然按原行为降级（``ADVISE`` + ``confidence=0``），不新开分支；
* 低置信度不硬猜：保留 ``confidence``，由上层问"要建议还是要执行"。
"""

from __future__ import annotations

import json

import pytest

from jagent.agent.llm_router import SYSTEM_PROMPT, LLMRouter
from jagent.contracts import IntentType
from jagent.ports import ModelReply


class FakeModel:
    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.calls = 0

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0) -> ModelReply:
        self.calls += 1
        return ModelReply(text=self._texts[min(self.calls, len(self._texts)) - 1])


def _reply(type_value: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {"type": type_value, "confidence": confidence, "reason": "判据"},
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# 1. 每个类一条
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("gather", IntentType.GATHER),
        ("execute", IntentType.EXECUTE),
        ("advise", IntentType.ADVISE),
    ],
)
def test_each_class_is_accepted(raw_type: str, expected: IntentType) -> None:
    decision = LLMRouter(FakeModel(_reply(raw_type))).route(
        text="随便问问", slots={}, turns=[]
    )

    assert decision.type is expected
    assert decision.confidence == pytest.approx(0.9)


def test_system_prompt_states_the_three_judgements() -> None:
    # 判据写在 prompt 里，而不是藏在本地关键词表。
    assert "gather" in SYSTEM_PROMPT
    assert "先取数再判断" in SYSTEM_PROMPT
    assert "有可机器验证的完成态" in SYSTEM_PROMPT
    assert "不需要外部事实" in SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# 2. 枚举外的值仍按原行为降级
# --------------------------------------------------------------------------- #

def test_unknown_type_keeps_the_existing_downgrade() -> None:
    decision = LLMRouter(FakeModel(_reply("chitchat", confidence=0.99))).route(
        text="今天天气不错", slots={}, turns=[]
    )

    # 原行为：不猜、不抛，降级成 confidence=0 的 ADVISE，理由里留痕。
    assert decision.type is IntentType.ADVISE
    assert decision.confidence == 0.0
    assert decision.reason.startswith("router_fallback:")
    assert "bad_type" in decision.reason


# --------------------------------------------------------------------------- #
# 3. 低置信度：保留结论，但要求上层先问路径
# --------------------------------------------------------------------------- #

def test_low_confidence_still_asks_advice_or_execute() -> None:
    decision = LLMRouter(FakeModel(_reply("gather", confidence=0.4))).route(
        text="这票能不能买", slots={}, turns=[]
    )

    assert decision.type is IntentType.GATHER
    assert decision.confidence == pytest.approx(0.4)
    # needs_path_choice 是"先问要建议还是要执行"的开关，阈值 0.7 不变。
    assert decision.needs_path_choice is True
    high = LLMRouter(FakeModel(_reply("gather", confidence=0.8))).route(
        text="这票能不能买", slots={}, turns=[]
    )
    assert high.needs_path_choice is False
