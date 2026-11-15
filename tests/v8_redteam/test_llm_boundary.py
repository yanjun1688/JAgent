"""I10/L2 · LLM 边界：模型只能起草，不能判定，也不能跳过澄清。

规格依据：
- §4.1 Router「confidence 低于阈值（0.7）不硬猜」；判据是完成态；
- §4.6「断言必须引用 Tool Registry 的 result_schema 里声明过的字段」；
- §4.4 合规校验四条规则（指令性 / 无来源数字 / 风险披露 / 槽位没齐给具体标的）；
- llm_planner.py 自己声明：工具名、needs ⊆ observations、步数上限、id 唯一。

Task 2 的四类攻击：缺槽位、恒真断言、空/超长/穿越计划、稳赚必涨。
"""

from __future__ import annotations

import json

import pytest

from jagent.agent.llm_planner import LLMPlanner, PlanDraftError
from jagent.agent.llm_router import LLMRouter
from jagent.agent.model import ScriptedModel
from jagent.contracts import (
    Advice,
    EventType,
    IntentDecision,
    IntentType,
    Plan,
    Step,
    Verify,
)
from jagent.gate.rules import AdviceGate
from jagent.harness.judge import JudgeVerdict
from jagent.harness.plancheck import check_plan
from jagent.harness.registry import build_default_registry


@pytest.fixture()
def tool_registry(tmp_path):
    ws = tmp_path / "toolws"
    ws.mkdir(parents=True, exist_ok=True)
    return build_default_registry(ws)


def _decision() -> IntentDecision:
    return IntentDecision(type=IntentType.EXECUTE, confidence=0.9)


def _steps_json(steps: list[dict]) -> str:
    return json.dumps({"steps": steps}, ensure_ascii=False)


def _one_step(tool: str = "fs.list", *, needs: list[str] | None = None, with_verify: bool = True) -> dict:
    step: dict = {
        "id": "s1",
        "tool": tool,
        "args": {"path": "."},
        "depends_on": [],
        "on_fail": "repair",
        "gated": False,
    }
    if with_verify:
        step["verify"] = {
            "expr": "len(entries) > 0",
            "needs": needs if needs is not None else ["entries"],
        }
    return step


# --------------------------------------------------------------------------- #
# Router：高置信度 execute 也不能跳过澄清
# --------------------------------------------------------------------------- #

def test_router_high_confidence_execute_does_not_claim_missing_slots() -> None:
    model = ScriptedModel(['{"type": "execute", "confidence": 0.99, "reason": "有完成态"}'])

    decision = LLMRouter(model).route(text="帮我看看这个", slots={}, turns=[])

    assert decision.type is IntentType.EXECUTE
    assert decision.confidence == pytest.approx(0.99)
    assert not decision.needs_path_choice
    # 路由层不声称缺槽位（那是第二张场景词表）；澄清由 planner 的 clarify 草稿现场问。
    assert decision.missing_params == ()


def test_session_asks_before_running_when_planner_needs_info(app_factory) -> None:
    """新链路：高置信度 execute 也不许跳过澄清——澄清权在 planner 的 clarify 模式。

    planner 判定信息不足时必须先问；planner 没给出计划之前，任何一方都不许建 run。
    """

    model = ScriptedModel([
        '{"type": "execute", "confidence": 0.99, "reason": "有完成态"}',
        '{"mode":"clarify","questions":[{"slot":"target_path",'
        '"text":"要看哪个目标？","options":["doc.md","tests/"]}]}',
    ])

    class _Judge:
        """可返回 REJECT 的 judge 替身；本用例只走到计划澄清，judge 不该被调用。"""

        model = "fake-judge"

        def judge(self, request):
            return None

    app = app_factory("router", model=model, judge=_Judge())

    outcome = app.session.handle_turn("帮我看看这个")

    assert outcome.status == "needs_input"
    assert outcome.question is not None and outcome.question.options
    assert app.world.runs == {}, "planner 给出计划之前不允许建 run"
    asked = [event for event in app.store.read() if event.type is EventType.CLARIFY_ASKED]
    assert asked and asked[-1].payload["kind"] == "planner_clarify"


def test_degraded_assembly_asks_first_and_never_builds_run(app_factory) -> None:
    """降级装配（无模型）：不猜意图，先问「建议还是执行」；且不许建 run。"""

    app = app_factory("router-degraded")  # model=None → AskRouter + UnavailablePlanner

    outcome = app.session.handle_turn("帮我看看这个")

    assert outcome.status == "needs_input"
    assert outcome.question is not None and outcome.question.options
    assert app.world.runs == {}, "planner 给出计划之前不允许建 run"

    second = app.session.handle_turn("帮我看看这个", {"__path__": "要执行"})
    assert second.status == "answered"          # 诚实说拆不了
    assert app.world.runs == {}
    asked = [event for event in app.store.read() if event.type is EventType.CLARIFY_ASKED]
    assert asked and asked[-1].payload["kind"] == "path_choice"


# --------------------------------------------------------------------------- #
# 恒真断言：当前能通过，但没有任何约束力
# --------------------------------------------------------------------------- #

def test_constant_true_assertion_passes_and_constrains_nothing(harness) -> None:
    from .conftest import make_runner, probe_tool

    spec, fn, _calls = probe_tool(name="probe", data={}, observations=("ok",))
    runner = make_runner(harness, tools=[(spec, fn)])
    plan = Plan(steps=(Step(id="s0", tool="probe", verify=Verify("1 == 1", needs=("ok",))),))

    outcome = runner.start_and_run(plan)

    assert outcome.status == "completed", "当前实现对恒真断言没有约束"
    verified = harness.of_type(EventType.STEP_VERIFIED)[-1]
    assert verified.payload["result"] == "pass"
    assert verified.payload["checks"][0]["expr"] == "1 == 1"


def test_plan_gate_rejects_constant_true_assertion(tool_registry) -> None:
    """已修复（本轮）：计划门对“断言里没有任何观测名”的表达式报 CONSTANT_ASSERTION。"""

    spec = tool_registry.get("fs.list")
    plan = Plan(
        steps=(
            Step(
                id="s1",
                tool="fs.list",
                args={"path": "."},
                verify=Verify("1 == 1", needs=("entries",)),
            ),
        )
    )

    issues = check_plan(plan, {"fs.list": spec})

    assert any("CONSTANT" in issue.code for issue in issues), (
        f"常量真断言没有约束力，应作为计划缺陷拒绝；实际 issues={[i.code for i in issues]}"
    )


# --------------------------------------------------------------------------- #
# 坏计划：空 / 50 步 / 路径穿越 / needs 未声明 / 缺 verify
# --------------------------------------------------------------------------- #

def test_empty_steps_are_rejected(tool_registry) -> None:
    model = ScriptedModel([_steps_json([])] * 3)
    planner = LLMPlanner(model, tool_registry)

    with pytest.raises(PlanDraftError) as info:
        planner.plan(decision=_decision(), slots={}, tools=tool_registry.descriptors(), text="跑测试")

    assert len(model.calls) == 1, "一次模型调用就够：重试在 Session，不在 Planner"
    assert info.value.issues, "坏草稿必须带非空 issues"


def test_fifty_step_plan_is_rejected(tool_registry) -> None:
    steps = [_one_step() for _ in range(50)]
    for index, step in enumerate(steps):
        step["id"] = f"s{index}"
    model = ScriptedModel([_steps_json(steps)] * 3)
    planner = LLMPlanner(model, tool_registry, max_steps=8)

    with pytest.raises(PlanDraftError):
        planner.plan(decision=_decision(), slots={}, tools=tool_registry.descriptors(), text="跑测试")


def test_traversal_tool_name_is_rejected(tool_registry) -> None:
    model = ScriptedModel([_steps_json([_one_step(tool="../../etc/passwd")])] * 3)
    planner = LLMPlanner(model, tool_registry)

    with pytest.raises(PlanDraftError):
        planner.plan(decision=_decision(), slots={}, tools=tool_registry.descriptors(), text="读文件")


def test_undeclared_observation_in_needs_is_rejected(tool_registry) -> None:
    bad = _one_step(needs=["entries", "secret"])
    model = ScriptedModel([_steps_json([bad])] * 3)
    planner = LLMPlanner(model, tool_registry)

    with pytest.raises(PlanDraftError):
        planner.plan(decision=_decision(), slots={}, tools=tool_registry.descriptors(), text="列目录")


def test_missing_verify_is_rejected(tool_registry) -> None:
    model = ScriptedModel([_steps_json([_one_step(with_verify=False)])] * 3)
    planner = LLMPlanner(model, tool_registry)

    with pytest.raises(PlanDraftError):
        planner.plan(decision=_decision(), slots={}, tools=tool_registry.descriptors(), text="列目录")


def test_bad_model_plan_never_reaches_the_executor(app_factory) -> None:
    """模型给出空计划：草稿被本地拒绝；Session 要么直接拒，要么降级并显式标记 degraded。"""

    app = app_factory("llm-empty")
    app.session.planner = LLMPlanner(ScriptedModel([_steps_json([])] * 3), app.registry)

    outcome = app.session.handle_turn("跑回归测试", {"repo_path": ".", "target": "tests"})

    assert outcome.status == "answered"
    proposed = [event for event in app.store.read() if event.type is EventType.PLAN_PROPOSED]
    for event in proposed:
        assert event.payload["plan"]["steps"], "被拒的空计划不能原样进执行环节"
    if outcome.run_id is not None:
        assert proposed and proposed[-1].payload.get("degraded") is True, (
            "换了计划必须留降级标记，否则用户以为跑的是模型给的计划"
        )


def test_session_hands_tool_descriptors_to_the_planner(app_factory) -> None:
    """回归护栏：Session 必须把 Tool Registry 的 descriptor 传给 Planner。

    这条曾经是 P1 缺口（tools=[]，任何 LLM 草稿都会被本地判成「未知工具」）；
    现在按「必须传」断言，防止回退。
    """

    seen: list[list] = []

    class ToolAwarePlanner:
        def plan(self, *, decision, slots, tools, text="", **kwargs):
            seen.append(list(tools))
            if not tools:
                raise PlanDraftError("no tool descriptors from Session")
            return Plan(
                steps=(
                    Step(
                        id="list",
                        tool="fs.list",
                        args={"path": "."},
                        verify=Verify("len(entries) > 0", needs=("entries",)),
                    ),
                )
            )

    app = app_factory("llm-tools", planner=ToolAwarePlanner())

    # 无模型 → AskRouter 会先问路径；答「要执行」后 planner 才被调用。
    first = app.session.handle_turn("跑回归测试")
    assert first.status == "needs_input"
    outcome = app.session.handle_turn("跑回归测试", {"__path__": "要执行"})

    assert seen and seen[0], "PlannerPort 的 tools 参数必须是 Tool Registry 的 descriptor 列表"
    assert outcome.run_id is not None


# --------------------------------------------------------------------------- #
# 建议合规：稳赚 / 必涨 / 槽位没齐给具体标的
# --------------------------------------------------------------------------- #

def _risky_advice(body: str) -> Advice:
    return Advice(
        body=body,
        risks=("市场风险：可能亏损。", "信息风险：可能滞后。"),
        disclaimer="以上不构成投资建议。",
        framework=(),
    )


class _RejectJudge:
    """T2 替身：策略评审否决；测试不联网、不依赖真实模型。"""

    model = "fake-judge"

    def judge(self, request):
        return JudgeVerdict.REJECT


def test_policy_judge_blocks_guaranteed_return_language() -> None:
    """v8.2：稳赚/必涨是语义判断，交 T2 judge（gate/policy.md）。

    T1 只做四条对账，不再为措辞写正则（v8.1 的 advice.directive 已删除）。
    """

    verdict = AdviceGate(judge=_RejectJudge()).check(
        _risky_advice("这只标的必涨，稳赚不赔，闭眼买。")
    )

    assert verdict.passed is False, "含「必涨」「稳赚」的建议必须被合规校验拦下"
    assert [violation.code for violation in verdict.violations] == ["policy_reject"]
    assert verdict.fallback is not None
    assert verdict.fallback.reason == "policy_reject"


def test_t1_alone_does_not_rule_on_semantics() -> None:
    """没有 judge 时 T1 只对账：语义判断缺失不等于"已放行"，但没有规则可命中。"""

    verdict = AdviceGate().check(_risky_advice("这只标的必涨，稳赚不赔，闭眼买。"))

    assert verdict.passed is True
    assert verdict.mode == "framework"
    # 没有 judge = 语义评审未发生：记 none（D-021），既不是通过也不是"判不了"。
    assert verdict.judge is not None
    assert verdict.judge.verdict == "none"
    assert verdict.judge.reason == "judge_not_configured"


def test_framework_only_flag_is_accepted_but_the_target_rule_is_gone() -> None:
    """advice.unaligned_target 随 v8.1 删除；framework_only 仅为旧调用点保留。"""

    verdict = AdviceGate().check(_risky_advice("600519 的走势偏强。"), framework_only=True)

    assert verdict.passed is True
    assert verdict.violations == ()
    assert verdict.mode == "framework"


def test_advice_decision_on_advice_draft_fails_closed(app_factory) -> None:
    """不过闸的建议草稿：fail-closed 落兜底，绝不让话术直接给用户。"""

    model = ScriptedModel([
        '{"type": "advise", "confidence": 0.9, "reason": "判断"}',
        (
            '{"mode":"advice","advice":{"body":"建议买入，稳赚不赔。",'
            '"risks":[],"framework":[],"sources":[],"disclaimer":"","info":[]}}'
        ),
    ])
    app = app_factory(
        "advice-fail-closed",
        model=model,
        gate=AdviceGate(judge=_RejectJudge()),
    )

    outcome = app.session.handle_turn("这支股票值得买吗")

    assert outcome.status == "answered"
    delivered = [e for e in app.store.read() if e.type.value == "advice.delivered"]
    assert delivered and delivered[-1].payload["mode"] == "fallback"
    risky = delivered[0]
    assert risky.payload["mode"] == "fallback"
    gates = [e for e in app.store.read() if e.type.value == "gate.checked"]
    assert gates and gates[-1].payload["pass"] is False
