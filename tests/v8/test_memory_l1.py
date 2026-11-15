"""记忆 L1 · 上下文内记忆接线（D-030：M1-A / M2-B / M3-A / M4-A）。

现状（修复前）：fold 的 ``turns`` 只存 ``text_hash``，router/planner 的 prompt
里给模型的是哈希和轮数——模型没有任何真实对话历史，跨轮连续性全靠 slots。

口径（D-030）：
- fold 维护 ``history``（role + 原文）：user 轮来自 ``TURN_RECEIVED.text``，
  assistant 回复来自 ``advice.delivered.advice.body`` 与
  ``report.rendered.result.summary``——数据已在事件日志里，零新存储；
- router 走既有 ``turns`` 形参（会话侧换成格式化的历史行）；
- planner 协议加 ``turns`` 形参，prompt 带「近期对话」段；
- 超出窗口的旧轮收敛为一条计数行（M4-A：规则生成、零模型）；
- 不含工具结果（M2-B：工具事实由 slots/证据承载）。
"""

from __future__ import annotations

from jagent.contracts import IntentDecision, IntentType, ReplyDraft
from jagent.wiring import build_app
from tests.support import DraftPlanner


class RecordingRouter:
    """FixedRouter 的记录版：把每次 route 收到的 turns/memory 原样留下来供断言。"""

    def __init__(self, decision: IntentDecision) -> None:
        self._decision = decision
        self.seen: list[list[str]] = []
        self.seen_memory: list[list[str]] = []

    def route(self, *, text, slots, turns, memory=()):
        self.seen.append([str(turn) for turn in turns])
        self.seen_memory.append([str(item) for item in memory])
        return self._decision


def _decision() -> IntentDecision:
    return IntentDecision(
        type=IntentType.ADVISE, confidence=0.9, missing_params=(), reason="recording"
    )


def test_router_sees_real_conversation_history(tmp_path) -> None:
    """Given 两轮对话（user 说了名字与偏好、assistant 各自回复过），
    When 第三轮路由，Then router 收到的 turns 是「user:/assistant:」原文行，
    且含前面两轮的内容——不再是 text_hash。"""
    router = RecordingRouter(_decision())
    app = build_app(
        tmp_path,
        planner=DraftPlanner([ReplyDraft("好的，记住了你的偏好。"), ReplyDraft("这次先不记。")]),
        router=router,
    )

    app.session.handle_turn("我叫张三，做保守投资的。")
    app.session.handle_turn("帮我看看 note.md 写了什么")
    app.session.handle_turn("给我个分析框架")

    assert len(router.seen) == 3
    history = router.seen[-1]
    joined = "\n".join(history)
    assert "张三" in joined, "user 轮原文没进历史（还是 text_hash）"
    assert "保守投资" in joined
    assert "好的，记住了你的偏好" in joined, "assistant 回复没进历史"
    assert "user:" in joined and "assistant:" in joined


def test_old_turns_collapse_into_a_count_line(tmp_path) -> None:
    """Given 超过窗口（8 条）的多轮对话，When 路由，
    Then 历史行 = 最近窗口原文 + 一条「更早 N 条已略」计数行（M4-A：规则生成），
    计数行不编造内容。"""
    router = RecordingRouter(_decision())
    drafts = [ReplyDraft(f"回复 {i}") for i in range(12)]
    app = build_app(tmp_path, planner=DraftPlanner(drafts), router=router)

    for i in range(6):
        app.session.handle_turn(f"第 {i} 轮的消息")

    history = router.seen[-1]
    assert history[0].startswith("（更早") and "已略" in history[0], f"缺旧轮收敛行：{history}"
    joined = "\n".join(history)
    assert "第 5 轮" in joined, "最近轮原文必须在窗口里"
    assert "第 0 轮" not in joined, "最早轮应被收敛掉"


def test_llm_planner_prompt_contains_recent_history(tmp_path) -> None:
    """Given planner 带历史轮调用（S-L1b），When 组 prompt，
    Then 「近期对话」段含对话原文——planner 也接线（M3-A）。"""
    import json

    from jagent.agent.llm_planner import LLMPlanner
    from jagent.harness.registry import build_default_registry

    from .test_llm_planner import FakeModel

    model = FakeModel(json.dumps({"mode": "reply", "text": "好的。"}))
    planner = LLMPlanner(model, build_default_registry(tmp_path / "ws"))
    planner.plan(
        decision=_decision(),
        slots={},
        tools=[],
        text="给我个分析框架",
        turns=["user: 我叫张三", "assistant: 你好张三"],
    )

    assert "近期对话" in model.prompts[0]
    assert "user: 我叫张三" in model.prompts[0]
    assert "assistant: 你好张三" in model.prompts[0]


def test_planner_without_turns_still_builds_prompt(tmp_path) -> None:
    """Given 不传 turns（旧调用方/协议默认值），When 组 prompt，
    Then 提示「这是第一轮」——协议扩展向后兼容，不误伤既有调用。"""
    import json

    from jagent.agent.llm_planner import LLMPlanner
    from jagent.harness.registry import build_default_registry

    from .test_llm_planner import FakeModel

    model = FakeModel(json.dumps({"mode": "reply", "text": "好的。"}))
    planner = LLMPlanner(model, build_default_registry(tmp_path / "ws"))
    planner.plan(decision=_decision(), slots={}, tools=[], text="你好")

    assert "这是第一轮" in model.prompts[0]
