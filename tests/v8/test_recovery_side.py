"""v8 恢复/投递侧：repair / report / human / fingerprint。

断言的是可执行的事实：修复只动该动的参数、报告只回放状态、审批只靠事件推进、
指纹只看内容。不 mock 事件流——用真 EventStore + Writer + fold 走一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jagent.contracts import (
    EventType,
    OnFail,
    Plan,
    RunVerdict,
    Step,
    StepReport,
    plan_to_dict,
)
from jagent.harness.fingerprint import changed, fingerprint
from jagent.harness.fold import fold
from jagent.harness.human import DECISIONS, HumanCenter
from jagent.harness.repair import CallableRepairer, HeuristicRepairer, NoopRepairer
from jagent.harness.report import Reporter
from jagent.harness.store import EventStore
from jagent.harness.writer import Writer

SESSION_ID = "s-recovery"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture()
def writer(tmp_path: Path):
    """真 store + 真 fold 出来的 Writer；每个测试一个独立 sqlite 文件。"""
    with EventStore(tmp_path / "events.db") as store:
        yield Writer(store, fold(store.read()), SESSION_ID)


def _freeze(writer: Writer, plan: Plan, run_id: str = "run-1"):
    """RUN_STARTED + PLAN_FROZEN，返回 fold 之后的 RunState。"""
    writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=run_id,
    )
    writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=run_id,
    )
    return writer.world.runs[run_id]


def _plan(*steps: Step) -> Plan:
    return Plan(steps=tuple(steps))


def _shell(step_id: str, timeout_s: float = 30.0) -> Step:
    return Step(id=step_id, tool="shell.exec", args={"cmd": "pytest -q", "timeout_s": timeout_s})


def _pass_step(writer: Writer, run_id: str, step_id: str) -> None:
    writer.step_to(run_id, step_id, "running", "start")
    writer.emit(
        EventType.STEP_FINISHED,
        {"step_id": step_id, "status": "ok", "artifact_refs": [f"art-{step_id}"]},
        run_id=run_id,
        step_id=step_id,
    )
    writer.emit(
        EventType.STEP_VERIFIED,
        {"step_id": step_id, "result": "pass", "checks": [{"expr": "ok", "passed": True}]},
        run_id=run_id,
        step_id=step_id,
    )
    writer.step_to(run_id, step_id, "passed", "verified")


def _fail_step(writer: Writer, run_id: str, step_id: str, failure_class: str = "assertion") -> None:
    writer.step_to(run_id, step_id, "running", "start")
    writer.emit(
        EventType.STEP_FINISHED,
        {"step_id": step_id, "status": "error", "artifact_refs": [], "error_code": "assertion_failed"},
        run_id=run_id,
        step_id=step_id,
    )
    writer.emit(
        EventType.FAILURE_CLASSIFIED,
        {"step_id": step_id, "class": failure_class, "error_code": "assertion_failed"},
        run_id=run_id,
        step_id=step_id,
    )
    writer.step_to(run_id, step_id, "failed", "verify failed")


# --------------------------------------------------------------------------- #
# HeuristicRepairer
# --------------------------------------------------------------------------- #

def test_heuristic_doubles_shell_timeout_and_leaves_other_steps_alone():
    plan = _plan(_shell("run_regression", 30.0), _shell("collect", 5.0))
    repaired = HeuristicRepairer().repair(
        plan=plan,
        step_id="run_regression",
        failure={"class": "transient", "reason": "timeout"},
        attempt=1,
    )

    assert repaired is not None
    assert repaired is not plan
    assert repaired.by_id()["run_regression"].args["timeout_s"] == 60.0
    assert repaired.by_id()["run_regression"].args["cmd"] == "pytest -q"  # 其它 args 原样
    assert repaired.steps[1] is plan.steps[1]                            # 未受影响的 step 不重建
    assert plan.steps[0].args["timeout_s"] == 30.0                       # 原计划未被改动
    assert repaired.version == plan.version
    assert repaired.hash() != plan.hash()


@pytest.mark.parametrize(("current", "expected"), [(100.0, 120.0), (120.0, 120.0)])
def test_heuristic_caps_timeout_at_120(current, expected):
    plan = _plan(_shell("run", current))
    repaired = HeuristicRepairer().repair(
        plan=plan, step_id="run", failure={"class": "transient", "reason": "timeout"}, attempt=2
    )

    assert repaired is not None
    assert repaired.by_id()["run"].args["timeout_s"] == expected


def test_heuristic_refuses_to_patch_over_evidence_missing():
    plan = _plan(_shell("run", 30.0))
    repaired = HeuristicRepairer().repair(
        plan=plan,
        step_id="run",
        failure={"class": "transient", "reason": "evidence_missing: no observations"},
        attempt=1,
    )

    assert repaired is None, "缺观测不能靠改计划糊过去"


@pytest.mark.parametrize(
    "failure",
    [
        {"class": "assertion", "reason": "exit_code != 0"},
        {"class": "deterministic", "reason": "permission_denied"},
        {},  # 没有 class：不知道失败性质，不猜
    ],
)
def test_heuristic_returns_none_for_everything_else(failure):
    plan = _plan(_shell("run", 30.0))
    assert HeuristicRepairer().repair(plan=plan, step_id="run", failure=failure, attempt=1) is None


@pytest.mark.parametrize(
    "step",
    [
        Step(id="run", tool="fs.read", args={"timeout_s": 30.0}),
        Step(id="run", tool="shell.exec", args={"cmd": "echo hi"}),
        Step(id="run", tool="shell.exec", args={"timeout_s": "not-a-number"}),
    ],
)
def test_heuristic_requires_shell_exec_with_numeric_timeout(step):
    plan = _plan(step)
    assert (
        HeuristicRepairer().repair(
            plan=plan, step_id="run", failure={"class": "transient", "reason": "timeout"}, attempt=1
        )
        is None
    )


def test_heuristic_ignores_unknown_step_id():
    plan = _plan(_shell("run", 30.0))
    assert (
        HeuristicRepairer().repair(
            plan=plan, step_id="ghost", failure={"class": "transient", "reason": "timeout"}, attempt=1
        )
        is None
    )


# --------------------------------------------------------------------------- #
# NoopRepairer / CallableRepairer
# --------------------------------------------------------------------------- #

def test_noop_repairer_never_repairs():
    plan = _plan(_shell("run", 30.0))
    assert NoopRepairer().repair(
        plan=plan, step_id="run", failure={"class": "transient", "reason": "timeout"}, attempt=1
    ) is None


def test_callable_repairer_forwards_exact_kwargs():
    seen: dict[str, object] = {}
    plan = _plan(_shell("run", 30.0))
    failure = {"class": "transient", "reason": "timeout"}

    def fn(**kwargs):
        seen.update(kwargs)
        return plan

    assert CallableRepairer(fn).repair(plan=plan, step_id="run", failure=failure, attempt=3) is plan
    assert seen == {"plan": plan, "step_id": "run", "failure": failure, "attempt": 3}


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #

def test_reporter_remaining_plan_and_summary_on_completed_run(writer):
    plan = _plan(
        Step(id="collect", tool="fs.read", args={"path": "a.txt"}, on_fail=OnFail.RETRY),
        _shell("run_regression"),
        Step(id="report", tool="report.render", args={}),
        Step(id="deploy", tool="ci.deploy", args={}, on_fail=OnFail.ASK_USER),
    )
    run = _freeze(writer, plan)
    for step_id in ("collect", "run_regression", "report"):
        _pass_step(writer, run.run_id, step_id)
    writer.step_to(run.run_id, "deploy", "skipped", "not required")
    writer.emit(EventType.RUN_COMPLETED, {"result": {"ok": True}}, run_id=run.run_id)

    result = Reporter().render(writer.world.runs[run.run_id])

    assert result.run_id == run.run_id
    assert result.verdict is RunVerdict.COMPLETED
    assert result.generated_by == "template"
    assert result.summary == "3/4 步通过"
    assert result.remaining_plan == ("deploy",), "未 passed 的 step 按 run.steps 顺序列出"
    assert [s.id for s in result.steps] == ["collect", "run_regression", "report", "deploy"]
    passed = result.steps[0]
    assert passed == StepReport(
        id="collect",
        status="passed",
        artifact_refs=("art-collect",),
        checks=({"expr": "ok", "passed": True},),
        evidence_seqs=passed.evidence_seqs,
    )
    assert len(passed.evidence_seqs) == 2, "STEP_FINISHED + STEP_VERIFIED 都是证据"
    assert result.budget_used["steps"] == writer.world.runs[run.run_id].budget["steps"]


def test_reporter_marks_failed_run_and_names_the_failure(writer):
    plan = _plan(
        _shell("collect"),
        Step(id="run_regression", tool="shell.exec", args={"cmd": "pytest"}),
        Step(id="report", tool="report.render", args={}),
    )
    run = _freeze(writer, plan)
    _pass_step(writer, run.run_id, "collect")
    _fail_step(writer, run.run_id, "run_regression")
    writer.step_to(run.run_id, "report", "skipped", "blocked by failure")
    writer.emit(EventType.RUN_FAILED, {"reason": "step run_regression failed"}, run_id=run.run_id)

    result = Reporter().render(writer.world.runs[run.run_id], generated_by="template")

    assert result.verdict is RunVerdict.FAILED
    assert result.summary == "1/3 步通过；失败：run_regression（assertion）"
    # remaining_plan = 所有未 passed 的 step（skipped 也算未通过：它没有产出）。
    assert result.remaining_plan == ("run_regression", "report")
    assert result.steps[1].status == "failed"


def test_reporter_treats_unfinished_run_as_cancelled(writer):
    plan = _plan(_shell("a"), _shell("b"))
    run = _freeze(writer, plan)

    result = Reporter().render(writer.world.runs[run.run_id])

    assert result.verdict is RunVerdict.CANCELLED
    assert result.remaining_plan == ("a", "b")
    assert result.summary == "0/2 步通过"


# --------------------------------------------------------------------------- #
# HumanCenter
# --------------------------------------------------------------------------- #

def test_human_request_suspends_run_and_returns_one_time_token(writer):
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer, ttl_s=60.0, clock=lambda: 1000.0)

    token = center.request(run.run_id, "deploy", "ph-1", context="push to prod")

    assert token == writer.world.runs[run.run_id].approval["token"]
    assert len(token) == 32 and set(token) <= set("0123456789abcdef")
    approval = center.pending(run.run_id)
    assert approval is not None
    assert approval["expires_at"] == 1060.0
    assert approval["kind"] == "gate"
    assert approval["context"] == "push to prod"
    assert writer.world.runs[run.run_id].status == "waiting_human"

    approval["token"] = "mutated"
    assert writer.world.runs[run.run_id].approval["token"] == token, "pending 返回副本"

    kinds = [e.type for e in writer.store.read(run_id=run.run_id)]
    assert kinds == [
        EventType.RUN_STARTED,
        EventType.PLAN_FROZEN,
        EventType.APPROVAL_REQUESTED,
        EventType.RUN_SUSPENDED,
    ]


def test_human_approve_resumes_run(writer):
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer, clock=lambda: 1000.0)
    token = center.request(run.run_id, "deploy", "ph-1")

    assert center.resolve(run.run_id, token, "approve", comment="lgtm") is True

    events = writer.store.read(run_id=run.run_id)
    types = [e.type for e in events]
    assert types[-3:] == [
        EventType.APPROVAL_RECEIVED,
        EventType.APPROVAL_RESOLVED,
        EventType.RUN_RESUMED,
    ]
    received, resolved, resumed = events[-3:]
    assert received.payload["decision"] == "approve"
    assert received.payload["comment"] == "lgtm"
    assert resolved.payload["applied"] is True
    assert resumed.payload["from_seq"] == resolved.seq
    assert center.pending(run.run_id) is None
    assert writer.world.runs[run.run_id].status == "running"


@pytest.mark.parametrize("decision", ["approve", "amend", "reject"])
def test_human_accepts_the_three_decisions(writer, decision):
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer)
    token = center.request(run.run_id, "deploy", "ph-1")

    assert center.resolve(run.run_id, token, decision) is True


def test_human_second_resolve_of_same_token_is_idempotent_and_false(writer):
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer)
    token = center.request(run.run_id, "deploy", "ph-1")

    assert center.resolve(run.run_id, token, "approve") is True
    before = len(writer.store.read(run_id=run.run_id))

    assert center.resolve(run.run_id, token, "approve") is False

    events = writer.store.read(run_id=run.run_id)
    assert len(events) == before + 1
    assert events[-1].type == EventType.APPROVAL_RESOLVED
    assert events[-1].payload["applied"] is False
    assert events[-1].payload["duplicate"] is True
    assert center.pending(run.run_id) is None
    assert writer.world.runs[run.run_id].status == "running", "重复解析不改状态"


def test_human_rejects_unknown_token_without_touching_approval(writer):
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer)
    token = center.request(run.run_id, "deploy", "ph-1")

    assert center.resolve(run.run_id, "deadbeef", "approve") is False
    assert writer.store.read(run_id=run.run_id)[-1].payload["duplicate"] is True
    assert center.pending(run.run_id)["token"] == token


def test_human_rejects_expired_token(writer):
    now = [1000.0]
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer, ttl_s=10.0, clock=lambda: now[0])
    token = center.request(run.run_id, "deploy", "ph-1")

    now[0] = 1010.0  # 正好到期不算过期（clock() > expires_at 才拒绝）
    assert center.pending(run.run_id) is not None

    now[0] = 1010.5
    assert center.resolve(run.run_id, token, "approve") is False

    last = writer.store.read(run_id=run.run_id)[-1]
    assert last.type == EventType.APPROVAL_RESOLVED
    assert last.payload["applied"] is False
    assert last.payload["reason"] == "expired"
    assert writer.world.runs[run.run_id].status == "waiting_human", "拒绝不等于恢复"
    assert center.pending(run.run_id) is not None, "过期拒绝不清 approval，等新请求覆盖"


def test_human_bad_decision_raises_and_writes_nothing(writer):
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer)
    token = center.request(run.run_id, "deploy", "ph-1")
    before = len(writer.store.read(run_id=run.run_id))

    for bad in ("yes", "APPROVE", "", "ok"):
        with pytest.raises(ValueError):
            center.resolve(run.run_id, token, bad)
    assert DECISIONS == {"approve", "amend", "reject"}
    assert len(writer.store.read(run_id=run.run_id)) == before
    assert center.pending(run.run_id)["token"] == token


def test_human_second_request_supersedes_first_token(writer):
    plan = _plan(Step(id="deploy", tool="ci.deploy", args={}, gated=True))
    run = _freeze(writer, plan)
    center = HumanCenter(writer)

    first = center.request(run.run_id, "deploy", "ph-1")
    second = center.request(run.run_id, "deploy", "ph-2")

    assert first != second
    assert center.resolve(run.run_id, first, "approve") is False
    assert center.pending(run.run_id)["token"] == second
    assert center.resolve(run.run_id, second, "approve") is True


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #

def test_fingerprint_changes_only_when_content_changes(tmp_path):
    target = tmp_path / "input.txt"
    target.write_text("v1", encoding="utf-8")

    before = fingerprint([target])
    assert before == fingerprint([target]), "同一内容必须得到同一个指纹"
    assert changed(before, before) is False

    target.write_text("v2", encoding="utf-8")
    after = fingerprint([target])

    assert after != before
    assert changed(before, after) is True


def test_fingerprint_records_missing_files_and_creation_changes_it(tmp_path):
    ghost = tmp_path / "not-yet.txt"

    missing = fingerprint([ghost])
    assert missing == fingerprint([ghost])
    assert missing != fingerprint([tmp_path / "other-missing.txt"]), "文件名参与指纹"

    ghost.write_text("now here", encoding="utf-8")
    assert changed(missing, fingerprint([ghost])) is True


def test_fingerprint_includes_extra_and_file_names(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("same", encoding="utf-8")
    b.write_text("same", encoding="utf-8")

    assert fingerprint([a], extra={"commit": "x"}) != fingerprint([a], extra={"commit": "y"})
    assert fingerprint([a]) == fingerprint([a], extra={})
    assert fingerprint([a]) != fingerprint([b]), "同内容不同文件不是同一个输入"
    assert fingerprint([a]) != fingerprint([a, b])


def test_fingerprint_is_read_only(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    target = src / "input.txt"
    target.write_text("data", encoding="utf-8")
    snapshot = sorted(p.name for p in src.iterdir())

    fingerprint([target], extra={"n": 1})

    assert sorted(p.name for p in src.iterdir()) == snapshot
    assert target.read_text(encoding="utf-8") == "data"
