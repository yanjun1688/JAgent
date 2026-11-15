"""Turn 层路由。

真实分类只有 `LLMRouter`：按「有没有可机器验证的完成态」分包 execute / advise。
这里只放无模型时的确定性兜底 `AskRouter`：

- 不再读关键词词表（按域词命中做分类等于把场景写死在代码里）；
- 不猜意图：固定 0.55 置信度，让上层走「要建议还是要执行」的路径选择；
- 同一个输入永远得到同一个决策（可复现）。
"""

from __future__ import annotations

from typing import Sequence

from ..contracts import IntentDecision, IntentType


class AskRouter:
    """无模型兜底：固定低置信度，把意图交给用户选择。"""

    def route(
        self,
        *,
        text: str,
        slots: dict | None = None,
        turns: Sequence[str] = (),
        memory: Sequence[str] = (),
    ) -> IntentDecision:
        return IntentDecision(
            type=IntentType.EXECUTE,
            confidence=0.55,
            reason="no deterministic intent signal; ask advice-or-execute",
        )
