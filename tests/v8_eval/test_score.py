"""attempt 评分与 pass^k 聚合（D-035 §4）。"""

from __future__ import annotations

from jagent.evaluation import (
    AttemptScore,
    corpus_aggregate,
    score_attempt,
    score_task,
    tasks_from_mapping,
)


def task_of(*checks) -> object:
    data = {
        "schema": "eval.tasks.v1",
        "task": {"t": {"description": "d", "turns": ["hi"], "checks": list(checks)}},
    }
    return tasks_from_mapping(data, tasks_hash="h").tasks[0]


HAPPY = ({"kind": "run_verdict", "expect": "completed"},)


def attempt(ok: bool) -> AttemptScore:
    return AttemptScore(
        verdict="pass" if ok else "fail",
        checks=(),
        metrics={},
        problems=(),
    )


def test_all_checks_pass_scores_pass(rig):
    rig.completed_run("r1")
    rig.turn_end("answered")
    task = task_of(*HAPPY, {"kind": "turn_outcome", "expect": "answered"})
    score = score_attempt(rig.events(), task)
    assert score.verdict == "pass"
    assert all(c.passed for c in score.checks)
    assert score.problems == ()


def test_failing_check_scores_fail_with_evidence(rig):
    rig.cancelled_run("r1")
    task = task_of(*HAPPY)
    score = score_attempt(rig.events(), task)
    assert score.verdict == "fail"
    failed = [c for c in score.checks if not c.passed]
    assert len(failed) == 1
    assert failed[0].kind == "run_verdict"
    assert failed[0].evidence["run_ids"] == ["r1"]


def test_replay_inconsistency_becomes_problem(rig, monkeypatch):
    """重放不一致是 runner 级问题：直接判 fail，不依赖 checks。"""
    rig.completed_run("r1")
    task = task_of(*HAPPY)
    from jagent import evaluation

    states = iter([evaluation.fold(rig.events()), "bogus"])
    monkeypatch.setattr(evaluation, "fold", lambda events: next(states))
    score = score_attempt(rig.events(), task)
    assert score.verdict == "fail"
    assert score.problems
    assert "replay" in score.problems[0]


def test_score_task_pass_k(rig):
    task = task_of(*HAPPY)

    def attempt(ok: bool):
        if ok:
            rig.completed_run(f"r-ok-{len(rig.events())}")
        else:
            rig.failed_run(f"r-bad-{len(rig.events())}")
        return score_attempt(rig.events(), task)

    passed = score_task([attempt(True), attempt(True)], task)
    assert passed.verdict == "passed"
    assert passed.k == task.k

    mixed = score_task([attempt(True), attempt(False)], task)
    assert mixed.verdict == "failed"  # pass^k：连续 k 次全过才算过


def test_corpus_aggregate_counts_rates():
    corpus = tasks_from_mapping(
        {
            "schema": "eval.tasks.v1",
            "task": {
                "a": {"description": "a", "turns": ["x"], "checks": HAPPY},
                "b": {"description": "b", "turns": ["x"], "checks": HAPPY},
                "c": {"description": "c", "turns": ["x"], "checks": HAPPY},
            },
        },
        tasks_hash="h",
    )
    scores = {
        task.task_id: score_task(
            [attempt(task.task_id != "c")], task
        )
        for task in corpus.tasks
    }
    block = corpus_aggregate(scores)
    assert block["tasks_total"] == 3
    assert block["tasks_passed"] == 2
    assert abs(block["task_pass_rate"] - 2 / 3) < 1e-9
    assert abs(block["attempt_pass_rate"] - 2 / 3) < 1e-9
