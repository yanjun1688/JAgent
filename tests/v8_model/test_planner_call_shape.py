"""Wave 2 · C2-fix · 工具选择是独立阶段，不是重试（D-014 / 架构 §4.6）。

这里钉住两件事：

* 工具数超过选择阈值时，`plan()` 的模型调用数是 **2**：第 1 次选工具，
  第 2 次起草；这是两个阶段，不是重试。
* Session 喂回的 `feedback` 必须以 `code: detail` 的形式出现在下一次起草
  prompt 里。**Session 链路**（`plan.rejected.issues` → 下一次 `plan()`）
  属于切片 D，本文件只测 Planner 侧的 prompt 渲染。
"""

from __future__ import annotations

import json
from pathlib import Path

from jagent.agent.llm_planner import LLMPlanner
from jagent.contracts import IntentDecision, IntentType, ToolResult, ToolSpec
from jagent.harness.plancheck import PlanIssue
from jagent.harness.registry import ToolRegistry
from jagent.ports import ModelReply

#: 默认选择阈值是 8；10 个工具必然触发工具选择阶段。
_ABOVE_THRESHOLD = 10


class _ScriptedModel:
    """按顺序吐预设文本，并保留 system / prompt 供断言。"""

    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.systems: list[str] = []
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
        self.systems.append(system)
        self.prompts.append(prompt)
        self.calls += 1
        return ModelReply(text=self._texts[min(self.calls, len(self._texts)) - 1])


def _decision() -> IntentDecision:
    return IntentDecision(type=IntentType.EXECUTE, confidence=0.9)


def _registry_with_tools(count: int) -> ToolRegistry:
    """注册 ``count`` 个真工具（不只是 descriptors），让选择阈值生效。"""

    registry = ToolRegistry()

    def fn(args: dict, workspace: Path, timeout: float) -> ToolResult:
        return ToolResult(ok=True)

    for index in range(count):
        registry.add(
            ToolSpec(
                name=f"tool_{index}",
                version="1",
                args_schema={"type": "object", "properties": {"q": {"type": "string"}}},
                description=f"第 {index} 个工具",
                observations=("ok",),
            ),
            fn,
        )
    return registry


def _plan_json(tool: str) -> str:
    return json.dumps(
        {
            "mode": "steps",
            "steps": [
                {
                    "id": "s1",
                    "tool": tool,
                    "args": {"q": "x"},
                    "depends_on": [],
                    "verify": {"expr": "ok == true", "needs": ["ok"]},
                }
            ],
        },
        ensure_ascii=False,
    )


def test_large_toolset_is_two_calls_selection_then_draft() -> None:
    """10 个工具（> 默认阈值 8）：第 1 次选工具，第 2 次起草，共 2 次。"""
    registry = _registry_with_tools(_ABOVE_THRESHOLD)
    model = _ScriptedModel('{"tools":["tool_3"]}', _plan_json("tool_3"))
    planner = LLMPlanner(model, registry)

    plan = planner.plan(
        decision=_decision(),
        slots={},
        tools=registry.descriptors(),
        text="用第 3 个工具查",
    )

    assert model.calls == 2, "选工具 + 起草各一次；不是重试"
    assert "工具选择器" in model.systems[0], "第 1 次必须是工具选择"
    assert "计划器" in model.systems[1], "第 2 次必须是起草"
    assert "工具目录" in model.prompts[0]
    assert "properties" not in model.prompts[0], "选择阶段只给名字 + 描述"
    assert "tool_3" in model.prompts[1]
    assert "tool_0" not in model.prompts[1], "起草阶段只带选中的工具"
    assert [step.tool for step in plan.steps] == ["tool_3"]


def test_feedback_carries_issue_code_and_detail_into_prompt() -> None:
    """feedback 的 code 与 detail 必须逐字进入下一次起草 prompt。

    Session 侧链路（`plan.rejected.issues` → 下一次 `plan()`）属于切片 D，
    不在这里测；这里只保证 Planner 收到 feedback 时会渲染。
    """
    registry = _registry_with_tools(2)  # 低于阈值：只有起草这一次模型调用
    model = _ScriptedModel(_plan_json("tool_0"))
    issue = PlanIssue(
        "NEEDS_NOT_PROVIDED",
        "s1",
        "断言要读 ['entries']，但 tool_0 只产出 ['ok']",
    )

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(),
        slots={},
        tools=registry.descriptors(),
        feedback=[issue],
        attempt=2,
    )

    assert model.calls == 1, "feedback 是上一轮给的，不该在本层再触发一次重试"
    assert [step.tool for step in plan.steps] == ["tool_0"]
    assert issue.code in model.prompts[0], "违规码必须进入 prompt"
    assert issue.detail in model.prompts[0], "违规详情必须逐字进入 prompt"
    assert f"{issue.code}: {issue.detail}" in model.prompts[0], "渲染格式必须是 code: detail"
