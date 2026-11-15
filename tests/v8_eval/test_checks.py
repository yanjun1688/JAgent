"""checks kind 的判定语义：每个 kind 的通过 / 失败两向 + 证据引用。"""

from __future__ import annotations

from jagent.evaluation import CheckDef, score_attempt, tasks_from_mapping


def task_of(*checks) -> object:
    data = {
        "schema": "eval.tasks.v1",
        "task": {
            "t": {
                "description": "d",
                "turns": ["hi"],
                "checks": checks,
            }
        },
    }
    return tasks_from_mapping(data, tasks_hash="h").tasks[0]


def run_check(rig, check: dict):
    """在 rig 当前事件流上跑单个 check，返回 CheckResult。"""
    task = task_of(check)
    return score_attempt(rig.events(), task).checks[0]


def make_checkdef(check: dict) -> CheckDef:
    return task_of(check).checks[0]


# --------------------------------------------------------------- run_verdict --


def test_run_verdict_pass_and_fail(rig):
    rig.completed_run("r1")
    result = run_check(rig, {"kind": "run_verdict", "expect": "completed"})
    assert result.passed
    assert result.observed == ["completed"]
    assert result.evidence["run_ids"] == ["r1"]
    assert result.evidence["seqs"]

    result = run_check(rig, {"kind": "run_verdict", "expect": "cancelled"})
    assert not result.passed
    assert result.observed == ["completed"]


def test_run_verdict_no_runs_fails(rig):
    result = run_check(rig, {"kind": "run_verdict", "expect": "completed"})
    assert not result.passed
    assert result.observed == []


def test_run_verdict_at_last(rig):
    rig.completed_run("r1")
    rig.cancelled_run("r2")
    check = {"kind": "run_verdict", "expect": "cancelled"}
    assert not run_check(rig, check).passed  # 默认 all：两个 run 都要满足

    check_last = {"kind": "run_verdict", "expect": "cancelled", "at": "last"}
    assert run_check(rig, check_last).passed


def test_run_verdict_cancelled(rig):
    rig.cancelled_run("r1")
    assert run_check(rig, {"kind": "run_verdict", "expect": "cancelled"}).passed


# -------------------------------------------------------------- turn_outcome --


def test_turn_outcome(rig):
    rig.completed_run("r1")
    rig.turn_end("answered")
    assert run_check(rig, {"kind": "turn_outcome", "expect": "answered"}).passed
    assert not run_check(rig, {"kind": "turn_outcome", "expect": "plan_rejected"}).passed


# ---------------------------------------------------------- steps_passed_min --


def test_steps_passed_min(rig):
    rig.completed_run("r1", steps=("s1", "s2"))
    assert run_check(rig, {"kind": "steps_passed_min", "count": 2}).passed
    assert not run_check(rig, {"kind": "steps_passed_min", "count": 3}).passed


# ------------------------------------------------------------ budget_within --


def test_budget_within_tokens_and_calls(rig):
    rig.model_call(purpose="planner", tokens_in=100, tokens_out=50)
    rig.model_call(purpose="router", tokens_in=10, tokens_out=5)
    ok = [
        {"kind": "budget_within", "tokens_in": 200, "tokens_out": 100, "model_calls": 2}
    ]
    for result in [run_check(rig, c) for c in ok]:
        assert result.passed
    fail = [
        {"kind": "budget_within", "tokens_in": 109},
        {"kind": "budget_within", "tokens_out": 54},
        {"kind": "budget_within", "model_calls": 1},
    ]
    for result in [run_check(rig, c) for c in fail]:
        assert not result.passed


def test_budget_within_wall_s(rig):
    rig.completed_run("r1")
    assert run_check(rig, {"kind": "budget_within", "wall_s": 60}).passed


# ----------------------------------------------------------------- 介入/澄清 --


def test_approvals_and_clarify(rig):
    rig.completed_run("r1", gated_step="s1")
    assert run_check(rig, {"kind": "approvals", "min": 1}).passed
    assert not run_check(rig, {"kind": "approvals", "max": 0}).passed

    rig.clarify(round=1)
    assert run_check(rig, {"kind": "clarify_asked", "min": 1}).passed
    assert run_check(rig, {"kind": "clarify_asked", "max": 1}).passed
    assert not run_check(rig, {"kind": "clarify_asked", "min": 2}).passed


# ---------------------------------------------------------- advice_delivered --


def test_advice_delivered_fields(rig):
    rig.advice_delivery(mode="grounded", citations=2, fallback=False)
    assert run_check(rig, {"kind": "advice_delivered", "mode": "grounded"}).passed
    assert run_check(rig, {"kind": "advice_delivered", "min_citations": 2}).passed
    assert run_check(rig, {"kind": "advice_delivered", "fallback": False}).passed
    assert not run_check(rig, {"kind": "advice_delivered", "min_citations": 3}).passed
    assert not run_check(rig, {"kind": "advice_delivered", "mode": "framework"}).passed


def test_advice_delivered_no_event_fails(rig):
    assert not run_check(rig, {"kind": "advice_delivered", "mode": "grounded"}).passed


def test_advice_delivered_last_wins(rig):
    rig.advice_delivery(mode="framework", citations=0)
    rig.advice_delivery(mode="grounded", citations=1)
    assert run_check(rig, {"kind": "advice_delivered", "mode": "grounded"}).passed


# --------------------------------------------------------------- gate_checked --


def test_gate_checked_fields(rig):
    rig.advice_delivery(
        mode="grounded", passed=True, judge="none", violations=("advice.no_citation",)
    )
    ok = run_check(
        rig,
        {
            "kind": "gate_checked",
            "pass": True,
            "mode": "grounded",
            "judge": "none",
            "rules_absent": ["advice.number_ungrounded"],
        },
    )
    assert ok.passed

    bad = run_check(
        rig,
        {"kind": "gate_checked", "rules_absent": ["advice.no_citation"]},
    )
    assert not bad.passed  # 违规在 violations 里，不能断言它缺席

    present = run_check(
        rig,
        {"kind": "gate_checked", "rules_present": ["advice.no_citation"]},
    )
    assert present.passed


def test_gate_checked_judge_reject(rig):
    rig.advice_delivery(mode="grounded", passed=False, judge="reject", fallback=True)
    assert run_check(rig, {"kind": "gate_checked", "judge": "reject"}).passed
    assert run_check(rig, {"kind": "gate_checked", "pass": False}).passed
    assert not run_check(rig, {"kind": "gate_checked", "pass": True}).passed


# ----------------------------------------------------------------- event_count --


def test_event_count(rig):
    rig.model_call()
    rig.model_call()
    assert run_check(rig, {"kind": "event_count", "type": "model.called", "exact": 2}).passed
    assert run_check(rig, {"kind": "event_count", "type": "model.called", "min": 1}).passed
    assert not run_check(rig, {"kind": "event_count", "type": "model.called", "max": 1}).passed


# ---------------------------------------------------------- step_started_max --


def test_step_started_max_counts_per_step(rig):
    rig.completed_run("r1", steps=("s1", "s2"))
    assert run_check(rig, {"kind": "step_started_max", "step_id": "s1", "max": 1}).passed
    assert not run_check(rig, {"kind": "step_started_max", "step_id": "s1", "max": 0}).passed
    missing = run_check(rig, {"kind": "step_started_max", "step_id": "s9", "max": 0})
    assert missing.passed  # 没启动过的步计数为 0


# ------------------------------------------------------------ replay_consistent --


def test_replay_consistent_passes_on_legal_stream(rig):
    rig.completed_run("r1")
    result = run_check(rig, {"kind": "replay_consistent"})
    assert result.passed
    assert result.observed  # 指纹非空


# ------------------------------------------------------------ verify_consistent --


def test_verify_consistent_passes_on_honest_stream(rig):
    """completed + 所有步 passed + 断言 pass：声称与证据一致（D-032）。"""
    rig.completed_run("r1")
    assert run_check(rig, {"kind": "verify_consistent"}).passed


def test_verify_consistent_flags_overclaim(rig):
    """声称 completed 但步骤停在 running：对账不一致 → check 失败。"""
    from jagent.contracts import EventType, Plan, Step, plan_to_dict

    plan = Plan(steps=(Step(id="s1", tool="fs.read"),))
    rig.emit(
        EventType.RUN_STARTED,
        {"run_id": "r1", "kind": "execute", "env": {"git_commit": "t", "config_hash": "h"}},
        run_id="r1",
    )
    rig.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id="r1",
    )
    rig.emit(
        EventType.STEP_STATE_CHANGED,
        {"step_id": "s1", "from": "pending", "to": "running", "reason": "start"},
        run_id="r1",
        step_id="s1",
    )
    rig.emit(EventType.RUN_COMPLETED, {"result": {"summary": "谎报"}}, run_id="r1")
    result = run_check(rig, {"kind": "verify_consistent"})
    assert not result.passed
    assert result.observed["problems"]


def test_scores_carry_attempt_metrics(rig):
    rig.turn_received("hi")
    rig.model_call(tokens_in=7, tokens_out=3)
    rig.completed_run("r1", steps=("s1",))
    rig.turn_end()
    task = task_of(
        {"kind": "run_verdict", "expect": "completed"},
        {"kind": "turn_outcome", "expect": "answered"},
    )
    score = score_attempt(rig.events(), task)
    assert score.verdict == "pass"
    assert score.problems == ()
    m = score.metrics
    assert m["tokens_in"] == 7
    assert m["tokens_out"] == 3
    assert m["model_calls"] == 1
    assert m["turns"] == 1
    assert m["runs"] == 1
    assert m["steps_passed"] == 1
    assert m["approvals"] == 0
    assert m["wall_s"] >= 0.0


def test_env_shape_from_conftest_used_in_streams(rig):
    """事件流的 run.started.env 是测试构造的——确保形状与生产 payload 一致。"""
    rig.completed_run("r1")
    started = [e for e in rig.events() if e.type.value == "run.started"]
    assert started[0].payload["env"] == {"git_commit": "test", "config_hash": "hash-test"}
