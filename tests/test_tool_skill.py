"""Feature: Skill 纳入 BaseTool（ADR-010 D-06）

行为分层（Given/When/Then）：
  1. Skill 实例 → to_definition 合成 ToolDefinition（name/schema/side_effects）
  2. Skill 带步骤 → run 编排子工具
  3. register_tool(Skill) → 注册为统一入口
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect
from harness.tools.registry import ToolRegistry
from harness.tools.skill import Skill


def _search_skill():
    def step_fetch(ctx, fns):
        return {"title": ctx["input"]["topic"]}

    return Skill(
        name="research_topic",
        description="Research a topic",
        input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
        steps=[step_fetch],
    )


class TestSkillBaseTool:
    def test_given_skill_when_to_definition_then_contract_synthesized(self):
        # Given 一个 Skill 实例
        skill = _search_skill()
        # When 合成 ToolDefinition
        td = skill.to_definition()
        # Then 契约字段正确
        assert td.name == "research_topic"
        assert td.side_effects == [SideEffect.EXTERNAL]
        assert td.input_schema["properties"]["topic"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_given_skill_steps_when_invoke_then_orchestrates_subtools(self):
        # Given 带步骤的 Skill
        skill = _search_skill()
        # When invoke
        result = await skill.invoke({"topic": "ai"})
        # Then 编排子步骤
        assert result["result"]["title"] == "ai"

    def test_given_skill_when_register_tool_then_registered(self):
        # Given Skill
        registry = ToolRegistry()
        # When 经 register_tool 注册
        registry.register_tool(_search_skill())
        # Then 注册为统一入口
        assert registry.get_tool_def("research_topic") is not None
        assert registry.get_tool_fn("research_topic") is not None
