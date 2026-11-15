"""JAgent v8 · 端到端测试。

覆盖 MVP 的五个承诺：
1. 确定性任务能完成，且完成由断言决定；
2. 断言失败 → 失败，不谎报完成；
3. 修复只重跑「本步 + 下游」，上游不重复执行；
4. 建议被合规拦住 → 打回 → 兜底，且兜底不含指令；
5. 重放两次同一指纹（零模型调用）；挂起后能恢复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jagent.agent.planner import ScriptedPlanner
from jagent.contracts import Advice, AdviceDraft, Budget, IntentType, Plan, Step, Verify, ToolResult
from jagent.gate.rules import AdviceGate
from jagent.harness.judge import JudgeVerdict
from jagent.harness.repair import CallableRepairer
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter

POOL = {
    "repo_path": ".",
    "target": "tests",
    "ticker": "示例标的",
    "horizon": "1年",
    "risk_profile": "均衡",
    "capital": "10万以内",
}


def run_turn(app, text: str, *, max_turns: int = 6):
    answers = None
    outcome = None
    for _ in range(max_turns):
        outcome = app.session.handle_turn(text, answers)
        if outcome.status != "needs_input":
            return outcome
        q = outcome.question
        if q.slot in POOL:
            answers = {q.slot: POOL[q.slot]}
        elif q.options:
            answers = {q.slot: q.options[0]}
        else:
            answers = {q.slot: "默认"}
    return outcome


@pytest.fixture()
def app(tmp_path: Path):
    # shell.exec 是不可逆工具：产品里默认要人工确认，测试里显式放行，避免每个用例都点一次。
    application = build_app(tmp_path, budget=Budget(auto_approve_tools=("shell.exec",)))
    tests = application.root / "ws" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    yield application
    application.close()


# --------------------------------------------------------------------------- #
# 1. 确定性任务完成
# --------------------------------------------------------------------------- #

def test_two_step_task_completes(tmp_path: Path):
    # 模板规划层已删除：这里用 ScriptedPlanner 固定「找测试 → 跑测试」两步，
    # 验证的是 Session → Runner → Executor → Verifier 的完整链路。
    tests = tmp_path / "ws" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    plan = Plan(steps=(
        Step(id="find_tests", tool="fs.list", args={"path": "tests"},
             verify=Verify("len(entries) > 0", needs=("entries",))),
        Step(id="run_tests", tool="shell.exec", args={"cmd": "python -m pytest tests -q"},
             depends_on=("find_tests",), verify=Verify("exit_code == 0", needs=("exit_code",))),
    ))
    app = build_app(
        tmp_path,
        planner=ScriptedPlanner([plan]),
        router=FixedRouter(),
        budget=Budget(auto_approve_tools=("shell.exec",)),
    )
    try:
        out = run_turn(app, "找一下 tests 然后跑回归测试")
    finally:
        app.close()
    assert out.status == "answered"
    assert out.result is not None
    assert out.result.verdict.value == "completed"
    assert {s.id for s in out.result.steps} == {"find_tests", "run_tests"}
    assert all(s.status == "passed" for s in out.result.steps)


# --------------------------------------------------------------------------- #
# 2. 断言失败 → FAILED
# --------------------------------------------------------------------------- #

def test_assertion_failure_is_failed_not_completed(tmp_path: Path):
    probe = (
        'python -c "import urllib.request,sys;'
        'print(urllib.request.urlopen(sys.argv[1],timeout=2).status)"'
    )
    plan = Plan(steps=(
        Step(id="probe_endpoint", tool="shell.exec",
             args={"cmd": f"{probe} http://127.0.0.1:1/nope"},
             verify=Verify("exit_code == 0", needs=("exit_code",))),
    ))
    app = build_app(
        tmp_path,
        planner=ScriptedPlanner([plan]),
        router=FixedRouter(),
        budget=Budget(auto_approve_tools=("shell.exec",)),
    )
    try:
        out = run_turn(app, "这个端点返回 code 200 吗 http://127.0.0.1:1/nope")
    finally:
        app.close()
    assert out.result is not None
    assert out.result.verdict.value == "failed"
    assert any(s.status == "failed" for s in out.result.steps)


# --------------------------------------------------------------------------- #
# 3. 修复只重跑本步 + 下游
# --------------------------------------------------------------------------- #

def test_repair_reruns_only_affected_branch(tmp_path):
    failing = Plan(steps=(
        Step(id="s1", tool="shell.exec", args={"cmd": 'python -c "print(1)"'},
             verify=Verify("exit_code == 0", needs=("exit_code",))),
        Step(id="s2", tool="shell.exec", args={"cmd": 'python -c "import sys;sys.exit(3)"'},
             depends_on=("s1",), verify=Verify("exit_code == 0", needs=("exit_code",))),
    ))
    fixed = Plan(steps=(
        failing.steps[0],
        Step(id="s2", tool="shell.exec", args={"cmd": 'python -c "print(2)"'},
             depends_on=("s1",), verify=Verify("exit_code == 0", needs=("exit_code",))),
    ))
    repairer = CallableRepairer(lambda **kw: fixed)
    app = build_app(
        tmp_path, repairer=repairer,
        budget=Budget(max_repairs=2, auto_approve_tools=("shell.exec",)),
    )
    out = app.runner.start_and_run(failing)
    assert out.status == "completed"
    run = app.world.runs[out.run_id]
    # 上游只执行了一次（复用了，没有被修复波及）
    assert run.steps["s1"].attempts == 1
    assert run.steps["s2"].attempts == 2
    app.close()


# --------------------------------------------------------------------------- #
# 4. 建议路径：被拦住 → 打回 → 兜底
# --------------------------------------------------------------------------- #

class _RejectJudge:
    """T2 替身：策略评审否决；测试不联网、不依赖真实模型。"""

    model = "fake-judge"

    def judge(self, request):
        return JudgeVerdict.REJECT


def test_advice_gate_blocks_then_falls_back(tmp_path):
    # 建议由 planner 的 advice 草稿产出（Propose 组件已删除）；
    # v8.2 起语义否决在 T2 judge（T1 只做四条对账），违规草稿过闸被拒 →
    # 一次裁决直接落兜底（fail-closed，不做多轮重试）。
    bad = Advice(body="建议买入，2024年涨幅可达8.5%，全仓进场。")
    app = build_app(
        tmp_path,
        planner=DraftPlanner([AdviceDraft(advice=bad)]),
        router=FixedRouter(IntentType.ADVISE),
        gate=AdviceGate(judge=_RejectJudge()),
    )
    try:
        out = run_turn(app, "这支股票值得买吗", max_turns=8)
        assert out.status == "answered"
        modes = [e.payload.get("mode") for e in app.store.read() if e.type.value == "advice.delivered"]
        assert modes == ["fallback"], modes
        # 兜底模板必须四段齐全，且不含指令
        advice = [e for e in app.store.read() if e.type.value == "advice.delivered"][-1].payload["advice"]
        assert advice["framework"] and len(advice["risks"]) >= 2 and advice["disclaimer"]
        assert "买入" not in advice["body"]
        # 合规校验确实拦过（而且写了事件）；judge 的否决码是 policy_reject
        rejects = [e for e in app.store.read() if e.type.value == "gate.checked" and not e.payload["pass"]]
        assert len(rejects) >= 1
        assert any(v["code"] == "policy_reject" for v in rejects[-1].payload["violations"])
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 5. 重放一致
# --------------------------------------------------------------------------- #

def test_replay_is_deterministic(app):
    run_turn(app, "跑回归测试")
    app.reload()
    first = app.world.fingerprint()
    app.reload()
    second = app.world.fingerprint()
    assert first == second
    assert app.store.max_seq() > 0


# --------------------------------------------------------------------------- #
# 6. 挂起 → 恢复
# --------------------------------------------------------------------------- #

def test_gated_step_suspends_and_resumes(tmp_path):
    app = build_app(tmp_path)
    plan = Plan(steps=(
        Step(id="write", tool="fs.write", args={"path": "x.txt", "content": "hi"},
             verify=Verify("ok", needs=("ok",)), gated=True),
    ))
    out = app.runner.start_and_run(plan)
    assert out.status == "waiting_human"
    run = app.world.runs[out.run_id]
    assert run.status == "waiting_human"
    assert run.approval and run.approval["token"]

    resumed = app.session.resume_run(out.run_id, "approve")
    assert resumed.status == "answered"
    assert app.world.runs[out.run_id].status == "completed"
    app.close()


def test_rejected_step_does_not_ask_again(tmp_path):
    """拒绝之后必须停，不能回到"再问一次确认"的死循环。"""
    app = build_app(tmp_path)
    plan = Plan(steps=(
        Step(id="write", tool="fs.write", args={"path": "x.txt", "content": "hi"},
             verify=Verify("ok", needs=("ok",)), gated=True),
    ))
    out = app.runner.start_and_run(plan)
    assert out.status == "waiting_human"

    rejected = app.session.resume_run(out.run_id, "reject")
    assert rejected.status == "answered"
    run = app.world.runs[out.run_id]
    assert run.status == "failed"
    assert run.approval is None
    assert run.steps["write"].status == "skipped"
    app.close()


# --------------------------------------------------------------------------- #
# 7. 瞬时故障重试
# --------------------------------------------------------------------------- #

def test_transient_retry_succeeds_within_budget(tmp_path):
    app = build_app(tmp_path, budget=Budget(transient_per_step=3))
    calls = {"n": 0}

    def flaky(args, workspace, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            return ToolResult(ok=False, error_code="timeout", error_message="boom")
        return ToolResult(ok=True, exit_code=0, stdout="ok")

    from jagent.contracts import Idempotency, SideEffect, ToolSpec

    app.registry.add(
        ToolSpec(name="flaky", version="1", args_schema={}, side_effect=SideEffect.NONE,
                 idempotency=Idempotency.NATURAL),
        flaky,
    )
    plan = Plan(steps=(
        Step(id="f", tool="flaky", args={}, verify=Verify("exit_code == 0", needs=("exit_code",))),
    ))
    out = app.runner.start_and_run(plan)
    assert out.status == "completed"
    assert calls["n"] == 3
    app.close()
