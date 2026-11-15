"""v8 架构符合性 · 结构 / 事件枚举级断言（只读、无模型、无网络、< 1s）。

这是 `docs/jagent-v8-conformance.md` 的可执行部分，只断言三件事：
1. 17 个组件各有一个家（§0.1 的 16 个框 + Session）；
2. 分层边界：`harness/` 不碰模型/agent，`agent/` 不写事件；
3. 事件词表：§3.2 的 18 条事件都在 `contracts.EventType` 里。

其中 3 个用例是 **xfail(strict=True)**，它们就是报告里的缺口本身：
    G-01 计划侧存在准入闸门（§4.4/§5/§9 明确"没有"）
    G-02 Session 直接写事件并决定路径（§0.2 三条硬禁止）
    G-10 `step.invalidated` 定义但从未 emit
缺口修好后这些用例会 XPASS(strict) 报警——届时删掉 xfail、把报告结论改成「守住」。

用法：.venv\\Scripts\\python.exe -m pytest tests/v8_arch -q
"""

from __future__ import annotations

import ast
import pathlib
import re


from jagent.contracts import REQUIRED_FIELDS, EventType

JAGENT = pathlib.Path(__file__).resolve().parents[2] / "jagent"

#: §0.1 的 16 个框 + §0.2 的 Session。审计口径：一个框至少一个模块认领。
#: 注：Clarify Loop 与 Propose 已并入 planner 的 clarify / advice 两种草稿模式
#: （模板建议层删除后不再有独立模块），两者的认领文件同为 agent/llm_planner.py。
COMPONENTS: dict[str, str] = {
    "1 Router": "agent/router.py",
    "2 Clarify Loop": "agent/llm_planner.py",
    "3 Propose": "agent/llm_planner.py",
    "4 合规校验": "gate/rules.py",
    "5 输出建议": "agent/delivery.py",
    "6 Planner": "agent/planner.py",
    "7 Executor": "harness/executor.py",
    "8 Verifier": "harness/verify.py",
    "9 失败分类器": "harness/classify.py",
    "10 Repair·Replan": "harness/repair.py",
    "11 Reporter": "harness/report.py",
    "12 人工确认中心": "harness/human.py",
    "13 Event Log": "harness/store.py",
    "14 Artifact Store": "harness/artifacts.py",
    "15 State Machine": "harness/statemachine.py",
    "16 Tool Registry": "harness/registry.py",
    "17 Session": "app/session.py",
}

#: §3.2「最小事件集」原样 18 条。
SPEC_VOCAB: frozenset[str] = frozenset(
    {
        "intent.routed",
        "clarify.asked",
        "slot.updated",
        "plan.proposed",
        "gate.checked",
        "plan.frozen",
        "step.started",
        "step.finished",
        "artifact.written",
        "step.verified",
        "failure.classified",
        "plan.amended",
        "approval.requested",
        "approval.received",
        "run.suspended",
        "run.resumed",
        "run.cancelled",
        "step.fingerprinted",
        "report.rendered",
        "advice.delivered",
        "memory.proposed",
        "memory.resolved",
        "model.called",
    }
)

#: 只有归约（fold）里出现过、生产代码没有 emit 点的事件（报告 G-10）。
KNOWN_DEAD: frozenset[str] = frozenset({"step.invalidated"})


def _sources() -> dict[pathlib.Path, str]:
    # utf-8-sig：`jagent/{__init__,app/__init__,harness/__init__}.py` 带 BOM，
    # 用 utf-8 读出来首字符是 U+FEFF，ast.parse 会直接 SyntaxError。
    return {p: p.read_text(encoding="utf-8-sig") for p in JAGENT.rglob("*.py")}


def _emit_sites(et: EventType) -> list[str]:
    """`EventType.X` 在「可能 emit」的文件里出现的位置。

    排除 contracts（定义）、fold（归约）、statemachine（只有一句死代码）——
    这三处出现 `EventType.X` 不构成"写了一条 X"。
    """
    skip = {"contracts.py", "fold.py", "statemachine.py"}
    hits: list[str] = []
    for path, text in _sources().items():
        if path.name in skip:
            continue
        for match in re.finditer(r"EventType\." + et.name + r"\b", text):
            line = text[: match.start()].count("\n") + 1
            hits.append(f"{path.relative_to(JAGENT.parent).as_posix()}:{line}")
    return hits


# --------------------------------------------------------------------------- #
# 1. 组件齐全
# --------------------------------------------------------------------------- #

def test_seventeen_components_each_have_a_home() -> None:
    missing = [f"{name} -> jagent/{rel}" for name, rel in COMPONENTS.items()
               if not (JAGENT / rel).is_file()]
    assert not missing, f"规格里的组件没有实现文件：{missing}"
    assert len(COMPONENTS) == 17, "16 个框 + Session = 17，不多不少"


# --------------------------------------------------------------------------- #
# 2. 分层边界
# --------------------------------------------------------------------------- #

def test_harness_never_imports_agent_or_a_model_client() -> None:
    """`harness/` 是确定性内核：不认模型、不认 agent、不直连网络（§0 分层）。

    注意要覆盖子包（`harness/ext/`）：只看 `path.parent.name == "harness"` 会漏掉
    子目录里的文件。
    """
    forbidden = (".agent", "agent.", "httpx", "openai", "anthropic", "requests")
    harness_root = JAGENT / "harness"
    offenders: list[str] = []
    for path, text in _sources().items():
        if not path.is_relative_to(harness_root):
            continue
        for node in ast.walk(ast.parse(text)):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if any(token in module for token in forbidden):
                    offenders.append(f"{path.name}:{node.lineno} -> {module}")
    assert not offenders, f"harness 越界依赖模型/网络/agent：{offenders}"


def test_agent_layer_never_writes_events() -> None:
    """`agent/` 只产出判定与文本，落盘由 Session/Writer 负责（§3.2「谁写」列）。"""
    offenders = [p.name for p, t in _sources().items()
                 if p.parent.name == "agent" and ("EventType" in t or "writer.emit" in t)]
    assert not offenders, f"agent 层直接写事件：{offenders}"


# --------------------------------------------------------------------------- #
# 3. 事件词表
# --------------------------------------------------------------------------- #

def test_every_spec_event_exists_in_contracts() -> None:
    implementation = {event.value for event in EventType}
    assert SPEC_VOCAB <= implementation, f"§3.2 的事件没实现：{SPEC_VOCAB - implementation}"


def test_required_fields_cover_every_event_type() -> None:
    """`validate_draft` 是唯一的必填字段闸门；少一个就等于该事件不设防。"""
    assert set(REQUIRED_FIELDS) == set(EventType)


def test_gate_profile_is_advice_only() -> None:
    """§4.4：v8 的合规校验只有建议侧 profile。

    v8.2 起 gate 允许接收 Plan，但只用于 ``missing(plan, evidence)`` 的
    evidence_requires 对账，不是计划准入门（grounding v3 §3 / D-001）：
    v8.1 的「规则文件里不许出现 plan 字样」已由「不许出现计划门」取代。
    """
    from jagent.gate.rules import AdviceGate

    assert AdviceGate().profile == "advice"
    rules = (JAGENT / "gate" / "rules.py").read_text(encoding="utf-8")
    assert "check_plan" not in rules, "合规校验里出现了计划草稿校验"
    assert "plancheck" not in rules, "合规校验里出现了计划草稿校验"
    assert ".plan(" not in rules, "合规校验里出现了计划门调用"


def test_no_unwired_extension_ports_inside_harness() -> None:
    """G-23 re-baseline（2026-11-14）：ext/ 停车场已清空——judge 与
    contract_snapshot 迁入 harness 正编，resources.py（未接线的投机组件）
    删除，容器落地时从 git 复活。「图上没有的组件一个都不加」回归字面。"""
    assert not (JAGENT / "harness" / "ext").exists()


def test_every_event_type_has_an_emit_point() -> None:
    dead = sorted(event.value for event in EventType if not _emit_sites(event))
    assert dead == [], f"定义了但从未 emit 的事件（应为 {sorted(KNOWN_DEAD)}）：{dead}"


def test_planner_to_executor_has_no_admission_gate() -> None:
    """G-01 re-baseline（2026-11-14，D-001/D-026）：Session 无准入门。

    plancheck.py 的合法用途收敛为两处——Planner 的草稿自检（D-001）与
    runner 的修复复核（D-026）；Session 侧不得在 start_and_run 之前调它，
    gate 侧也不得（合规校验只审建议，不审计划）。
    """
    session = (JAGENT / "app" / "session.py").read_text(encoding="utf-8")
    assert "check_plan" not in session
    gate_rules = (JAGENT / "gate" / "rules.py").read_text(encoding="utf-8")
    assert "check_plan" not in gate_rules


def test_session_writes_events_only_through_the_writer() -> None:
    """G-02 re-baseline（§0.2 2026-11-02 修订 / D-033）：Session 承担 turn 编排
    并 emit 事件是规格明文接受的设计（TurnController 被 §8 删除后编排的落点）。

    仍然成立的两条边界：
    - Session 不直接写 EventStore——写一律经 Writer（本断言）；
    - Session 不做"对错"判定（判定在 Verifier / 合规校验）——设计约束，
      由判定组件的归属测试（G-01 / gate 系列）间接守住。
    """
    session = (JAGENT / "app" / "session.py").read_text(encoding="utf-8")
    assert "store.append" not in session, "Session 直接写 EventStore（绕过 Writer）"
    assert "EventStore(" not in session, "Session 自建 EventStore"
    assert "writer.emit" in session, "Session 的编排事实应经 Writer 落事件"
