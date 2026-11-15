"""Turn 层：规划端口与替身。

- `PlanningError`：认不出这次请求该怎么处理。宁可以说「我拆不出来」，也不要用
  noop 假装完成——那会让用户以为事办了（这是 v8 契约的一部分）。
- `UnavailablePlanner`：没接模型时的默认规划器。域知识不写死在代码里：
  计划必须由 `LLMPlanner` 起草（模型给草稿，代码校验），断言引用工具声明过的观测名。
- `ScriptedPlanner`：测试替身，按顺序吐出预设计划。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..contracts import Plan


class PlanningError(RuntimeError):
    """认不出这个请求该用哪套动作。

    宁可说「我拆不出来」，也不要用一个 noop 假装完成——那会让用户以为事办了。
    """


class UnavailablePlanner:
    """没接模型时的默认规划器：诚实地说这次拆不了。"""

    has_model = False

    def plan(self, **_kwargs: Any) -> Plan:
        raise PlanningError("no model configured; cannot decompose this request into steps")


class ScriptedPlanner:
    """测试用：按顺序吐出预设计划。"""

    def __init__(self, plans: Sequence[Plan]) -> None:
        self._plans = list(plans)
        self.calls = 0

    def plan(self, **kwargs: Any) -> Plan:
        self.calls += 1
        index = min(self.calls - 1, len(self._plans) - 1)
        return self._plans[index]
