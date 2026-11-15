"""v8 · LLM Router 契约测试。

FakeModel 只按顺序吐预置文本，不 import jagent.agent.model；
所有"模型不可用 / 乱码 / 字段非法"都必须降级而不是抛强异常。

覆盖：正常 JSON、confidence clamp、type 降级、URL 单步、模型不可用、
且路由不声称缺什么槽位（澄清由 planner 的 clarify 模式现场决定）。

建议产出的合规契约（必须过 AdviceGate）见 test_llm_integration（planner 的 advice 草稿）。
"""

from __future__ import annotations

from jagent.agent.llm_router import LLMRouter
from jagent.contracts import IntentType, Slot
from jagent.ports import ModelReply, NoModel


class FakeModel:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
        self.prompts.append(prompt)
        return ModelReply(text=self.replies.pop(0))


def _execute_slots() -> dict[str, Slot]:
    return {
        "repo_path": Slot(name="repo_path", value="D:/Project/JAgent"),
        "target": Slot(name="target", value="tests/v8"),
    }


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #

def test_router_reads_valid_json_execute() -> None:
    model = FakeModel('{"type": "execute", "confidence": 0.9, "reason": "有退出码可断言"}')

    decision = LLMRouter(model).route(text="帮我跑一遍回归", slots=_execute_slots(), turns=[])

    assert decision.type is IntentType.EXECUTE
    assert decision.confidence == 0.9
    assert decision.missing_params == ()
    assert decision.single_step is False
    assert "退出码" in decision.reason


def test_router_does_not_claim_missing_params() -> None:
    reply = '{"type": "execute", "confidence": 0.9, "missing_params": ["nothing"]}'
    model = FakeModel(reply)

    decision = LLMRouter(model).route(text="帮我跑一遍回归", slots={}, turns=[])

    assert decision.type is IntentType.EXECUTE
    # 路由层不声称「缺什么」——那会变成第二张场景词表；缺什么由 planner 的
    # clarify 草稿现场问（模型按 args schema 生成，代码只留痕）。
    assert decision.missing_params == ()


def test_router_garbage_falls_back_to_zero_confidence() -> None:
    decision = LLMRouter(FakeModel("我觉得这事儿说不清")).route(text="随便聊聊", slots={}, turns=[])

    assert decision.type is IntentType.ADVISE
    assert decision.confidence == 0.0
    assert decision.reason.startswith("router_fallback:parse_error")
    assert decision.needs_path_choice is True
    assert decision.missing_params == ()


def test_router_clamps_out_of_range_confidence() -> None:
    high = LLMRouter(FakeModel('{"type": "advise", "confidence": 3}')).route(
        text="这只标的值得买吗", slots={}, turns=[]
    )
    low = LLMRouter(FakeModel('{"type": "advise", "confidence": -2}')).route(
        text="这只标的值得买吗", slots={}, turns=[]
    )

    assert high.confidence == 1.0
    assert low.confidence == 0.0


def test_router_unknown_type_falls_back() -> None:
    decision = LLMRouter(FakeModel('{"type": "unknown", "confidence": 0.9}')).route(
        text="这只标的值得买吗", slots={}, turns=[]
    )

    assert decision.type is IntentType.ADVISE
    assert decision.confidence == 0.0
    assert decision.reason.startswith("router_fallback:bad_type")


def test_router_url_sets_single_step_only_for_execute() -> None:
    text = "打开 http://localhost:8000/health，返回 code 200 吗"
    execute = LLMRouter(FakeModel('{"type": "execute", "confidence": 0.8, "reason": "看状态码"}')).route(
        text=text, slots=_execute_slots(), turns=[]
    )
    advise = LLMRouter(FakeModel('{"type": "advise", "confidence": 0.9}')).route(
        text=text, slots={}, turns=[]
    )

    assert execute.single_step is True
    assert "single_step" in execute.reason
    assert advise.single_step is False


def test_router_model_unavailable_falls_back_without_raising() -> None:
    decision = LLMRouter(NoModel()).route(text="帮我跑一遍回归", slots={}, turns=[])

    assert decision.type is IntentType.ADVISE
    assert decision.confidence == 0.0
    assert decision.reason.startswith("router_fallback:model_unavailable")
