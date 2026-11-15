"""G-05 恢复对账 · 独立验收。

需求（来自用户与审计 G-05）：
1. 计划冻结时记录环境指纹；恢复时重算，变了才能让步骤失效。
2. 失效必须是局部的——没受影响的已通过步骤不许重跑（用工具调用计数断言）。
3. 人工批准绑定的 payload 必须被执行前复核：批准后参数被改 → 不许执行，必须重新请求批准。
4. 崩溃在 step.started 之后（没有 step.finished）：读类可安全重跑；shell.exec 必须转人工。
5. 重试同一步时 idempotency_key 不变。
6. 重复 resume 幂等（不重复执行、不重复发 token）。
"""

from __future__ import annotations

import dataclasses

from jagent.contracts import EventType, Idempotency, Plan, SideEffect, Step, Verify, plan_to_dict

from .conftest import (
    env_fingerprint,
    frozen_event,
    make_runner,
    start_run,
)

# 已知仍未修的用例用 xfail(strict=False)：修好后自动变 XPASS，不制造假红。

# --------------------------------------------------------------------------- #
# 1 · 计划冻结时记录环境指纹
# --------------------------------------------------------------------------- #

def test_g05_01_plan_frozen_records_env_fingerprint(bench):
    """冻结必须留下"当时的环境长什么样"，否则恢复时无从判断变没变。

    走生产冻结路径（`Runner.start_and_run`）：第一步 gated，所以只冻结、不执行。
    """
    (bench.ws / "input.txt").write_text("v1", encoding="utf-8")
    bench.add_tool("probe")
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="probe",
                args={"path": "input.txt"},
                env_probes=("input.txt",),
                gated=True,
            ),
        )
    )
    runner = make_runner(bench)

    a = runner.start_and_run(plan)
    assert a.status == "waiting_human", "gated 首步应当挂起，不执行"
    first = env_fingerprint(bench, a.run_id)

    (bench.ws / "input.txt").write_text("v2", encoding="utf-8")
    b = runner.start_and_run(plan)
    after_change = env_fingerprint(bench, b.run_id)

    c = runner.start_and_run(plan)
    stable = env_fingerprint(bench, c.run_id)

    hashes = {
        frozen_event(bench, run_id).payload["plan_hash"]
        for run_id in (a.run_id, b.run_id, c.run_id)
    }
    assert len(hashes) == 1, "计划本身没变，plan_hash 不该跟着环境一起变"

    assert first != after_change, "环境内容变了，冻结记录的环境指纹没有跟着变"
    assert after_change == stable, "环境没变，重算的环境指纹必须稳定"


# --------------------------------------------------------------------------- #
# 2 · 失效是局部的
# --------------------------------------------------------------------------- #

def test_g05_02_env_change_reruns_only_affected_steps(bench):
    """挂起期间环境变了：受影响的步骤（和下游）重跑，无关的已通过步骤一次都不许再跑。"""
    (bench.ws / "input.txt").write_text("v1", encoding="utf-8")
    calls = {
        name: bench.add_tool(name)
        for name in ("probe_a", "probe_b", "probe_c", "probe_gate")
    }
    plan = Plan(
        steps=(
            Step(id="c", tool="probe_c", args={}, verify=Verify("ok == true")),
            Step(
                id="a",
                tool="probe_a",
                args={"path": "input.txt"},
                env_probes=("input.txt",),
                verify=Verify("ok == true"),
            ),
            Step(id="b", tool="probe_b", args={}, depends_on=("a",), verify=Verify("ok == true")),
            Step(
                id="gate",
                tool="probe_gate",
                args={},
                depends_on=("b",),
                gated=True,
                verify=Verify("ok == true"),
            ),
        )
    )
    runner = make_runner(bench)

    first = runner.start_and_run(plan)

    assert first.status == "waiting_human"
    assert {name: len(c) for name, c in calls.items()} == {
        "probe_a": 1, "probe_b": 1, "probe_c": 1, "probe_gate": 0,
    }

    # 人还在看审批单的时候，被探测的输入变了。
    (bench.ws / "input.txt").write_text("v2", encoding="utf-8")
    pending = bench.human.pending(first.run_id)
    assert pending is not None
    assert bench.human.resolve(first.run_id, pending["token"], "approve") is True

    resumed = runner.resume(first.run_id, approved_steps=frozenset({"gate"}))

    assert resumed.status == "completed", f"恢复后没能完成：{resumed.reason}"
    assert len(calls["probe_a"]) >= 2, "环境变了，受影响步骤必须重跑"
    assert len(calls["probe_c"]) == 1, "没受影响的已通过步骤被重跑了（失效不是局部的）"
    assert len(calls["probe_gate"]) == 1


def test_g05_02b_drift_is_scoped_to_the_step_whose_probe_changed(bench):
    """两个步骤探测不同文件：只改一个文件时，另一个步骤没受影响，不许重跑。"""
    (bench.ws / "input.txt").write_text("v1", encoding="utf-8")
    (bench.ws / "other.txt").write_text("v1", encoding="utf-8")
    calls = {
        name: bench.add_tool(name)
        for name in ("probe_x", "probe_y", "probe_gate")
    }
    plan = Plan(
        steps=(
            Step(
                id="x",
                tool="probe_x",
                args={"path": "input.txt"},
                env_probes=("input.txt",),
                verify=Verify("ok == true"),
            ),
            Step(
                id="y",
                tool="probe_y",
                args={"path": "other.txt"},
                env_probes=("other.txt",),
                verify=Verify("ok == true"),
            ),
            Step(
                id="gate",
                tool="probe_gate",
                args={},
                depends_on=("x", "y"),
                gated=True,
                verify=Verify("ok == true"),
            ),
        )
    )
    runner = make_runner(bench)

    first = runner.start_and_run(plan)
    assert first.status == "waiting_human"
    assert len(calls["probe_x"]) == 1 and len(calls["probe_y"]) == 1

    # 只动 x 声明的探针文件；y 的探针一个字都没变。
    (bench.ws / "input.txt").write_text("v2", encoding="utf-8")
    pending = bench.human.pending(first.run_id)
    assert pending is not None
    assert bench.human.resolve(first.run_id, pending["token"], "approve") is True

    resumed = runner.resume(first.run_id, approved_steps=frozenset({"gate"}))

    assert resumed.status == "completed", f"恢复后没能完成：{resumed.reason}"
    assert len(calls["probe_x"]) >= 2, "自己探测的文件变了，必须重跑"
    assert len(calls["probe_y"]) == 1, "y 的探针没变，却被连带重跑了（失效不是按 env_probes 判的）"


# --------------------------------------------------------------------------- #
# 3 · 批准绑定的 payload 必须执行前复核
# --------------------------------------------------------------------------- #
def test_g05_03_payload_change_after_approval_forces_new_approval(bench):
    """人批准的是"当时那份参数"；参数被改过就必须重新问，不能拿旧批准跑新命令。"""
    calls = bench.add_tool("probe_deploy")
    plan = Plan(
        steps=(Step(id="deploy", tool="probe_deploy", args={"target": "staging"}, gated=True),)
    )
    runner = make_runner(bench)

    first = runner.start_and_run(plan)
    assert first.status == "waiting_human"
    pending = bench.human.pending(first.run_id)
    assert pending is not None
    approved_hash = pending["payload_hash"]
    token = pending["token"]
    assert bench.human.resolve(first.run_id, token, "approve") is True

    # 批准之后、执行之前，参数被改（修复器/操作者/上游都能做到这件事）。
    tampered = Step(id="deploy", tool="probe_deploy", args={"target": "prod"}, gated=True)
    tampered_plan = dataclasses.replace(plan, steps=(tampered,), version=plan.version + 1)
    bench.writer.emit(
        EventType.PLAN_AMENDED,
        {
            "plan": plan_to_dict(tampered_plan),
            "plan_hash": tampered_plan.hash(),
            "version": tampered_plan.version,
            "reason": "operator_edit",
            "invalidated": ["deploy"],
        },
        run_id=first.run_id,
    )

    resumed = runner.resume(first.run_id, approved_steps=frozenset({"deploy"}))

    assert calls == [], "批准之后参数被改，仍然执行了没被复核的 payload"
    assert resumed.status == "waiting_human", "payload 变了必须重新请求批准，而不是继续跑"
    requests = bench.of_type(EventType.APPROVAL_REQUESTED, run_id=first.run_id)
    assert len(requests) == 2, f"应当出现第二次批准请求，实际 {len(requests)} 次"
    assert requests[-1].payload["payload_hash"] != approved_hash
    assert bench.human.pending(first.run_id)["token"] != token


# --------------------------------------------------------------------------- #
# 4 · 崩溃在 step.started 之后
# --------------------------------------------------------------------------- #

def test_g05_04a_crashed_idempotent_read_is_rerun(bench):
    """读类工具天然幂等：日志说"开始了但没结束"时可以安全重跑，不该把 run 判死。"""
    calls = bench.add_tool("probe_read")
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="probe_read",
                args={"path": "input.txt"},
                verify=Verify("ok == true"),
            ),
        )
    )
    start_run(bench, "run_crash_read", plan)
    bench.writer.emit(
        EventType.STEP_STARTED,
        {"step_id": "read", "attempt": 1, "idempotency_key": "crashed-key"},
        run_id="run_crash_read",
        step_id="read",
    )
    # 崩溃事实：日志里 step 处于 running，但没有对应的 STEP_FINISHED。
    assert bench.world.runs["run_crash_read"].steps["read"].status == "running"

    outcome = make_runner(bench).resume("run_crash_read")

    assert outcome.status == "completed", f"读类工具崩溃后应能安全重跑：{outcome.reason}"
    assert len(calls) == 1, "读类工具必须被重新执行一次（幂等，可安全重放）"
    started = bench.of_type(EventType.STEP_STARTED, run_id="run_crash_read")
    assert len(started) == 2, "重跑必须先写 STEP_STARTED（write-ahead），不能先执行后记账"
    assert len(bench.of_type(EventType.STEP_FINISHED, run_id="run_crash_read")) == 1


def test_g05_04b_crashed_shell_step_escalates_to_human(bench):
    """shell.exec 不可幂等：不知道有没有跑过就绝不能猜，必须转人工确认。"""
    calls = bench.add_tool(
        "shell.exec",
        side_effect=SideEffect.IRREVERSIBLE,
        idempotency=Idempotency.NONE,
    )
    plan = Plan(steps=(Step(id="run", tool="shell.exec", args={"cmd": "deploy-prod"}),))
    start_run(bench, "run_crash_shell", plan)
    bench.writer.emit(
        EventType.STEP_STARTED,
        {"step_id": "run", "attempt": 1, "idempotency_key": "crashed-key"},
        run_id="run_crash_shell",
        step_id="run",
    )
    # auto_approve 只覆盖"执行前的闸门"，不覆盖"崩溃后状态未知"。
    runner = make_runner(bench, auto_approve={"shell.exec"})

    outcome = runner.resume("run_crash_shell")

    assert calls == [], "不可幂等的 shell.exec 崩溃后不许直接重跑"
    assert outcome.status == "waiting_human", f"必须转人工确认，实际 {outcome.status}"
    requests = bench.of_type(EventType.APPROVAL_REQUESTED, run_id="run_crash_shell")
    assert [r.payload["step_id"] for r in requests] == ["run"], (
        "崩溃后的重新确认必须指向同一步（上一次结果未知，不能拿旧批准放行）"
    )


# --------------------------------------------------------------------------- #
# 5 · 重试同一步时 idempotency_key 不变
# --------------------------------------------------------------------------- #

def test_g05_05_idempotency_key_stable_across_retries(bench):
    calls = bench.add_tool("flaky", fail_times=1, error_code="timeout")
    plan = Plan(steps=(Step(id="flaky", tool="flaky", args={"k": "v"}, verify=Verify("ok == true")),))

    outcome = make_runner(bench).start_and_run(plan)

    assert outcome.status == "completed", f"瞬时失败应该被重试掉：{outcome.reason}"
    assert len(calls) == 2, "瞬时失败应当重试一次"
    started = bench.of_type(EventType.STEP_STARTED, run_id=outcome.run_id)
    assert [e.payload["attempt"] for e in started] == [1, 2]
    keys = {e.payload["idempotency_key"] for e in started}
    assert len(keys) == 1, f"同一步重试的 idempotency_key 变了：{keys}"


# --------------------------------------------------------------------------- #
# 6 · 重复 resume 幂等
# --------------------------------------------------------------------------- #

def test_g05_06a_resume_after_completion_executes_nothing(bench):
    calls = bench.add_tool("probe_once")
    plan = Plan(steps=(Step(id="once", tool="probe_once", args={}, verify=Verify("ok == true")),))
    runner = make_runner(bench)

    first = runner.start_and_run(plan)
    assert first.status == "completed"

    for _ in range(3):
        again = runner.resume(first.run_id)
        assert again.status == "completed"

    assert len(calls) == 1, "终态之后 resume 又把步骤跑了一遍"
    assert len(bench.of_type(EventType.STEP_STARTED, run_id=first.run_id)) == 1
    assert len(bench.of_type(EventType.RUN_COMPLETED, run_id=first.run_id)) == 1


def test_g05_06b_repeated_resume_while_waiting_does_not_issue_new_token(bench):
    calls = bench.add_tool("probe_deploy")
    plan = Plan(
        steps=(Step(id="deploy", tool="probe_deploy", args={"target": "prod"}, gated=True),)
    )
    runner = make_runner(bench)

    first = runner.start_and_run(plan)
    assert first.status == "waiting_human"
    token = bench.human.pending(first.run_id)["token"]

    for _ in range(2):
        again = runner.resume(first.run_id)
        assert again.status == "waiting_human"

    assert calls == [], "还没批准就执行了不可逆步骤"
    requests = bench.of_type(EventType.APPROVAL_REQUESTED, run_id=first.run_id)
    assert len(requests) == 1, f"重复 resume 重复发了 token（{len(requests)} 次批准请求）"
    assert bench.human.pending(first.run_id)["token"] == token
