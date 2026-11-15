"""v8 · LLM 版规划器测试。

模型只提供文本，合法性全部由 `LLMPlanner` 本地判定。这里逐条覆盖校验规则、
失败清单（`issues`）、`ModelUnavailable` 的 outage 标记，以及两处本地强制
（`gated` / `args`）。重试循环在 Session，本文件不测重试。

`FakeModel` 是本文件私有的假模型，故意不 import `jagent.agent.model`：
规划器只依赖 `ModelPort` 协议，测试也只依赖协议。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jagent.agent.llm_planner import LLMPlanner, PlanDraftError
from jagent.contracts import (
    IntentDecision,
    IntentType,
    OnFail,
    Plan,
    Slot,
    Step,
    Verify,
)
from jagent.harness.plancheck import PlanIssue
from jagent.harness.registry import ToolRegistry, build_default_registry
from jagent.ports import ModelReply, ModelUnavailable


class FakeModel:
    """10 行假模型：按顺序吐预设文本，并把每次 prompt 留下来供断言。"""

    def __init__(self, *texts: str) -> None:
        self._texts = list(texts) or [""]
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
        self.prompts.append(prompt)
        self.calls += 1
        return ModelReply(text=self._texts[min(self.calls, len(self._texts)) - 1])


class _DownModel:
    """模型不可用：每次调用都抛 `ModelUnavailable`。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
        self.calls += 1
        raise ModelUnavailable("upstream is down")


@pytest.fixture()
def registry(tmp_path: Path) -> ToolRegistry:
    return build_default_registry(tmp_path)


def _decision(*, single_step: bool = False, target_tool: str | None = None) -> IntentDecision:
    return IntentDecision(
        type=IntentType.EXECUTE,
        confidence=0.9,
        single_step=single_step,
        target_tool=target_tool,
    )


def _plan_json(*steps: dict[str, object]) -> str:
    return json.dumps({"steps": list(steps)}, ensure_ascii=False)


def _step(
    step_id: str,
    tool: str,
    *,
    args: dict[str, object] | None = None,
    expr: str = "ok",
    needs: list[str] | None = None,
    depends_on: list[str] | None = None,
    **extra: object,
) -> dict[str, object]:
    step: dict[str, object] = {
        "id": step_id,
        "tool": tool,
        "args": {} if args is None else args,
        "depends_on": [] if depends_on is None else depends_on,
        "verify": {"expr": expr, "needs": ["ok"] if needs is None else needs},
    }
    step.update(extra)
    return step


# --------------------------------------------------------------------------- #
# 合法路径
# --------------------------------------------------------------------------- #

def test_valid_two_step_plan_parses_and_hashes(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(
            _step(
                "list",
                "fs.list",
                args={"path": "tests"},
                expr="len(entries) > 0",
                needs=["entries"],
            ),
            _step(
                "run",
                "shell.exec",
                args={"cmd": "python -m pytest -q"},
                depends_on=["list"],
                expr="exit_code == 0",
                needs=["exit_code"],
            ),
        )
    )

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(),
        slots={"repo_path": Slot(name="repo_path", value="repo-root")},
        tools=registry.descriptors(),
        text="跑一遍测试",
    )

    assert [step.id for step in plan.steps] == ["list", "run"]
    assert plan.steps[0].verify == Verify(expr="len(entries) > 0", needs=("entries",))
    assert plan.steps[1].depends_on == ("list",)
    assert plan.steps[1].on_fail is OnFail.REPAIR
    assert plan.steps[0].gated is False
    assert [step.id for step in plan.order()] == ["list", "run"]
    assert len(plan.hash()) == 64
    assert model.calls == 1

    # prompt 里该有的东西：意图、槽位、工具 descriptors、硬约束
    assert "跑一遍测试" in model.prompts[0]
    assert "repo-root" in model.prompts[0]
    assert "fs.list" in model.prompts[0]
    assert "observations" in model.prompts[0]
    assert "最多 8 步" in model.prompts[0]


def test_defaults_and_args_are_preserved(registry: ToolRegistry) -> None:
    args = {"path": "notes/a.txt", "max_lines": 3}
    model = FakeModel(
        _plan_json(
            _step(
                "read",
                "fs.read",
                args=args,
                expr="len(content) > 0",
                needs=["content"],
            )
        )
    )

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(), slots={}, tools=registry.descriptors()
    )

    assert plan.steps[0].args == args
    assert plan.steps[0].args is not args  # 原样保留，但不是同一个对象
    assert json.dumps(plan.steps[0].args)  # 一定是 JSON 可编码的 dict
    assert plan.steps[0].on_fail is OnFail.REPAIR  # on_fail 缺省 repair
    assert plan.steps[0].gated is False  # gated 缺省 False
    assert plan.steps[0].depends_on == ()
    assert plan.steps[0].required is True


def test_fenced_json_is_accepted(registry: ToolRegistry) -> None:
    body = _plan_json(
        _step("read", "fs.read", args={"path": "a.txt"}, expr="len(content) > 0", needs=["content"])
    )
    model = FakeModel("```json\n" + body + "\n```")

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(), slots={}, tools=registry.descriptors()
    )

    assert [step.id for step in plan.steps] == ["read"]
    assert model.calls == 1


def test_replan_carries_previous_plan_and_failure(registry: ToolRegistry) -> None:
    previous = Plan(
        steps=(Step(id="old_step", tool="fs.list", verify=Verify(expr="ok", needs=("ok",))),)
    )
    model = FakeModel(
        _plan_json(
            _step("new_step", "fs.list", args={"path": "."}, expr="len(entries) > 0", needs=["entries"])
        )
    )

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(),
        slots={},
        tools=registry.descriptors(),
        replan_of=previous,
        failure={"error_code": "not_found", "step_id": "old_step"},
    )

    assert [step.id for step in plan.steps] == ["new_step"]
    assert "old_step" in model.prompts[0]
    assert "not_found" in model.prompts[0]


# --------------------------------------------------------------------------- #
# 本地校验：工具 / id / 依赖 / 断言 / 步数 / 空计划
# --------------------------------------------------------------------------- #

def test_unknown_tool_is_rejected_in_one_model_call(registry: ToolRegistry) -> None:
    model = FakeModel(_plan_json(_step("nuke", "fs.rm", args={"path": "x"})))

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert model.calls == 1, "一次模型调用就够：重试在 Session，不在 Planner"
    assert len(model.prompts) == 1
    assert "fs.rm" in info.value.reason
    assert "fs.rm" in info.value.raw
    assert info.value.issues, "违规清单必须交给 Session，不能只剩一条文案"
    assert "fs.rm" in info.value.issues[0].detail
    assert isinstance(info.value, PlanDraftError)


def test_dependency_cycle_is_rejected(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(
            _step("a", "fs.list", args={"path": "."}, expr="len(entries) > 0", needs=["entries"], depends_on=["b"]),
            _step("b", "fs.list", args={"path": "."}, expr="len(entries) > 0", needs=["entries"], depends_on=["a"]),
        )
    )

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert "成环" in info.value.reason


def test_dependency_on_missing_step_is_rejected(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(
            _step("run", "shell.exec", args={"cmd": "echo hi"}, expr="exit_code == 0", needs=["exit_code"], depends_on=["ghost"])
        )
    )

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert "不存在的 step 'ghost'" in info.value.reason


def test_broken_verify_expression_is_rejected(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(_step("run", "shell.exec", args={"cmd": "echo hi"}, expr="exit_code ==", needs=["exit_code"]))
    )

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert "语法错误" in info.value.reason


def test_constant_assertion_draft_is_rejected_by_own_check(registry: ToolRegistry) -> None:
    """恒真断言在草稿自检阶段就被拒，不靠执行后的 Verifier 才发现。"""
    model = FakeModel(
        _plan_json(_step("read", "fs.read", args={"path": "x.txt"}, expr="1 == 1", needs=["ok"]))
    )

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert [issue.code for issue in info.value.issues] == ["CONSTANT_ASSERTION"]


def test_undeclared_observation_is_rejected(registry: ToolRegistry) -> None:
    # fs.read 只声明 ok/content，entries 是 fs.list 的观测。
    model = FakeModel(
        _plan_json(
            _step("read", "fs.read", args={"path": "a.txt"}, expr="len(entries) > 0", needs=["entries"])
        )
    )

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert "未声明的观测" in info.value.reason
    assert "entries" in info.value.reason


def test_invalid_json_is_rejected_in_one_model_call(registry: ToolRegistry) -> None:
    """坏 JSON：一次调用、一次解析就抛，且 issues 非空（Session 可直接落 plan.rejected）。"""
    model = FakeModel("这不是 JSON")

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert model.calls == 1
    assert len(model.prompts) == 1
    assert info.value.raw == "这不是 JSON"
    assert "不是合法 JSON" in info.value.reason
    assert info.value.issues, "解析失败也要给出 PlanIssue，不能只靠异常文案"


def test_feedback_is_fed_back_into_the_next_prompt(registry: ToolRegistry) -> None:
    fixed = _plan_json(
        _step("read", "fs.read", args={"path": "a.txt"}, expr="len(content) > 0", needs=["content"])
    )
    model = FakeModel(fixed)

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(),
        slots={},
        tools=registry.descriptors(),
        feedback=[PlanIssue("NEEDS_NOT_PROVIDED", "read", "未声明的观测 'entries'")],
        attempt=2,
    )

    assert model.calls == 1, "feedback 是上一轮给的，不该在本层再触发一次内部重试"
    assert [step.id for step in plan.steps] == ["read"]
    # prompt 必须带上「上一轮的违规清单」，且点名具体名字
    assert "上一轮输出的违规清单" in model.prompts[0]
    assert "NEEDS_NOT_PROVIDED" in model.prompts[0]
    assert "entries" in model.prompts[0]


def test_too_many_steps_is_rejected(registry: ToolRegistry) -> None:
    steps = [
        _step(f"s{index}", "fs.list", args={"path": "."}, expr="len(entries) > 0", needs=["entries"])
        for index in range(3)
    ]
    model = FakeModel(_plan_json(*steps))

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry, max_steps=2).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert "超过上限 2" in info.value.reason
    assert model.calls == 1
    assert info.value.issues


def test_empty_steps_is_rejected(registry: ToolRegistry) -> None:
    model = FakeModel(json.dumps({"steps": []}))

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert "steps 不能为空" in info.value.reason
    assert model.calls == 1
    assert info.value.issues


def test_non_dict_args_is_rejected(registry: ToolRegistry) -> None:
    model = FakeModel(_plan_json(_step("read", "fs.read", args=["a.txt"], needs=["content"])))

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert "args 必须是 JSON 对象" in info.value.reason


# --------------------------------------------------------------------------- #
# 本地强制：irreversible → gated
# --------------------------------------------------------------------------- #

def test_irreversible_tool_is_forced_gated(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(
            _step(
                "run",
                "shell.exec",
                args={"cmd": "echo hi"},
                expr="exit_code == 0",
                needs=["exit_code"],
                on_fail="retry",
                gated=False,  # 模型说不要 gate；本地必须改成 True
            )
        )
    )

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(), slots={}, tools=registry.descriptors()
    )

    assert plan.steps[0].gated is True
    assert plan.steps[0].on_fail is OnFail.RETRY


def test_reversible_tool_keeps_gate_from_draft(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(
            _step("write", "fs.write", args={"path": "a.txt", "content": "x"}, gated=True)
        )
    )

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(), slots={}, tools=registry.descriptors()
    )

    assert plan.steps[0].gated is True


# --------------------------------------------------------------------------- #
# 单步意图
# --------------------------------------------------------------------------- #

def test_single_step_intent_asks_for_one_step(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(_step("read", "fs.read", args={"path": "a.txt"}, expr="len(content) > 0", needs=["content"]))
    )

    plan = LLMPlanner(model, registry).plan(
        decision=_decision(single_step=True), slots={}, tools=registry.descriptors()
    )

    assert [step.id for step in plan.steps] == ["read"]
    assert "单步意图" in model.prompts[0]
    assert "只能有 1 个元素" in model.prompts[0]


def test_single_step_intent_rejects_multi_step_draft(registry: ToolRegistry) -> None:
    model = FakeModel(
        _plan_json(
            _step("a", "fs.list", args={"path": "."}, expr="len(entries) > 0", needs=["entries"]),
            _step("b", "fs.list", args={"path": "."}, expr="len(entries) > 0", needs=["entries"], depends_on=["a"]),
        )
    )

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(single_step=True), slots={}, tools=registry.descriptors()
        )

    assert "只能输出 1 步" in info.value.reason


# --------------------------------------------------------------------------- #
# 模型不可用：转成 PlanDraftError(outage=True)，保留原因
# --------------------------------------------------------------------------- #

def test_model_unavailable_becomes_plan_draft_error(registry: ToolRegistry) -> None:
    model = _DownModel()

    with pytest.raises(PlanDraftError) as info:
        LLMPlanner(model, registry).plan(
            decision=_decision(), slots={}, tools=registry.descriptors()
        )

    assert not isinstance(info.value, ModelUnavailable)
    assert isinstance(info.value.__cause__, ModelUnavailable), "原始异常不能被吞掉"
    assert info.value.outage is True, "模型不可用必须与坏草稿区分"
    assert "模型不可用" in info.value.reason
    assert "upstream is down" in info.value.reason
    assert info.value.raw == ""
    assert model.calls == 1  # 没有模型时不重试


# --------------------------------------------------------------------------- #
# 两段式：先选工具（只看名字 + 描述），再起草（只带选中工具的 schema）
# --------------------------------------------------------------------------- #

def _many_tools(count: int = 10) -> list[dict]:
    from jagent.contracts import ToolSpec

    return [
        ToolSpec(
            name=f"tool_{index}",
            version="1",
            args_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            description=f"第 {index} 个工具",
            observations=("ok",),
        ).descriptor()
        for index in range(count)
    ]


def test_tool_selection_narrows_the_planner_prompt(registry: ToolRegistry) -> None:
    tools = _many_tools(10)
    model = FakeModel(
        '{"tools":["tool_3"]}',
        _plan_json(_step("s1", "tool_3", args={"q": "x"})),
    )
    planner = LLMPlanner(model, registry, select_threshold=8)

    plan = planner.plan(decision=_decision(), slots={}, tools=tools, text="用第 3 个工具查")

    assert isinstance(plan, Plan)
    assert [step.tool for step in plan.steps] == ["tool_3"]
    # 选择阶段只给名字 + 描述，不给 schema
    assert "properties" not in model.prompts[0]
    # 起草阶段只带选中的工具
    assert "tool_3" in model.prompts[1]
    assert "tool_0" not in model.prompts[1]


def test_tool_selection_falls_back_to_all_when_names_do_not_match(registry: ToolRegistry) -> None:
    tools = _many_tools(10)
    model = FakeModel(
        '{"tools":["does_not_exist"]}',
        _plan_json(_step("s1", "tool_7", args={"q": "x"})),
    )
    planner = LLMPlanner(model, registry, select_threshold=8)

    plan = planner.plan(decision=_decision(), slots={}, tools=tools, text="随便查")

    assert [step.tool for step in plan.steps] == ["tool_7"]
    assert "tool_0" in model.prompts[1], "选择不可用时必须退回全量"


def test_tool_selection_is_skipped_for_small_tool_sets(registry: ToolRegistry) -> None:
    tools = _many_tools(3)
    model = FakeModel(_plan_json(_step("s1", "tool_1", args={"q": "x"})))
    planner = LLMPlanner(model, registry, select_threshold=8)

    planner.plan(decision=_decision(), slots={}, tools=tools, text="查")

    assert model.calls == 1, "工具少时不该多打一次模型"


def test_model_unavailable_during_tool_selection_is_wrapped(registry: ToolRegistry) -> None:
    """选工具也是模型调用：第一次就不可用时，同样转成 outage，不能再打一次。"""
    tools = _many_tools(10)
    model = _DownModel()
    planner = LLMPlanner(model, registry, select_threshold=8)

    with pytest.raises(PlanDraftError) as info:
        planner.plan(decision=_decision(), slots={}, tools=tools, text="用第 3 个工具查")

    assert model.calls == 1, "选工具失败后不该再尝试起草"
    assert info.value.outage is True
    assert isinstance(info.value.__cause__, ModelUnavailable)
    assert "模型不可用" in info.value.reason
