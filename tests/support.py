"""测试共用替身。

两个桩把「路由」与「规划」从测试里解耦：

- `FixedRouter`：固定返回一个 IntentDecision，避免用例被 AskRouter 的
  「建议还是执行」问题截住（那是无模型降级路径的行为，不是被测对象）。
- `DraftPlanner`：`draft_capable` 的 planner 替身，按顺序吐出预置草稿
  （ClarifyDraft / ReplyDraft / AdviceDraft / Plan），用来测 Session 的分流。
"""

from __future__ import annotations

from typing import Any, Sequence

from jagent.contracts import IntentDecision, IntentType, PlannerDraft


class FixedRouter:
    """固定决策的路由桩：同一个输入永远同一个决策。"""

    def __init__(
        self,
        intent: IntentType = IntentType.EXECUTE,
        confidence: float = 0.9,
    ) -> None:
        self._decision = IntentDecision(
            type=intent, confidence=confidence, reason="fixed_router"
        )

    def route(
        self,
        *,
        text: str,
        slots: dict | None = None,
        turns: Sequence[str] = (),
        memory: Sequence[str] = (),
    ) -> IntentDecision:
        return self._decision


class DraftPlanner:
    """按顺序返回预置草稿的 planner 替身；用尽后重复最后一个。"""

    draft_capable = True

    def __init__(self, drafts: Sequence[PlannerDraft]) -> None:
        self._drafts = list(drafts)
        self.calls = 0

    def plan(self, **kwargs: Any) -> PlannerDraft:
        index = min(self.calls, len(self._drafts) - 1)
        self.calls += 1
        return self._drafts[index]
