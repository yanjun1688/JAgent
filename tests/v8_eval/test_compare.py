"""对照协议：任务对齐、verdict 变化、四维 delta、指纹差异、fail-closed。"""

from __future__ import annotations

import pytest

from jagent.evaluation import EvalError, compare_reports

FP_BASE = {
    "git_commit": "aaa111",
    "config_hash": "cfg-1",
    "eval_tasks_hash": "tasks-1",
    "config_files": {"config/prompts/router.md": "h1", "config/eval/tasks.toml": "t1"},
}


def report(fp: dict, tasks: dict) -> dict:
    return {
        "fingerprint": fp,
        "corpus": {
            "tasks_total": len(tasks),
            "tasks_passed": sum(1 for t in tasks.values() if t["verdict"] == "passed"),
            "task_pass_rate": 0.0,
            "attempt_pass_rate": 0.0,
        },
        "tasks": tasks,
    }


def task_entry(verdict: str, metrics: dict) -> dict:
    return {
        "description": "d",
        "tags": (),
        "k": 1,
        "verdict": verdict,
        "attempts": [{"index": 0, "verdict": "pass" if verdict == "passed" else "fail", "checks": [], "metrics": metrics, "problems": ()}],
    }


M_OK = {"tokens_in": 100, "tokens_out": 50, "model_calls": 2, "turns": 1, "runs": 1, "steps_passed": 2, "wall_s": 1.0, "approvals": 0}
M_SLOW = M_OK | {"tokens_in": 180, "steps_passed": 1, "wall_s": 2.5}


def test_compare_aligns_tasks_and_lists_changes():
    a = report(FP_BASE, {"t1": task_entry("passed", M_OK), "t2": task_entry("failed", M_OK)})
    b_fp = FP_BASE | {"git_commit": "bbb222", "config_hash": "cfg-2", "config_files": {"config/prompts/router.md": "h2", "config/eval/tasks.toml": "t1"}}
    b = report(b_fp, {"t1": task_entry("passed", M_SLOW), "t2": task_entry("passed", M_OK)})
    out = compare_reports(a, b)
    assert out["regressions"] == []
    assert [t["task_id"] for t in out["improvements"]] == ["t2"]
    delta = out["task_deltas"]["t1"]["metrics"]
    assert delta["tokens_in"]["delta"] == 80
    assert delta["steps_passed"]["delta"] == -1
    assert "config/budget.toml" not in out["config_changed_files"]
    assert out["config_changed_files"] == ["config/prompts/router.md"]


def test_compare_regression_detected():
    a = report(FP_BASE, {"t1": task_entry("passed", M_OK)})
    b = report(FP_BASE | {"git_commit": "bbb222"}, {"t1": task_entry("failed", M_OK)})
    out = compare_reports(a, b)
    assert [t["task_id"] for t in out["regressions"]] == ["t1"]
    assert out["improvements"] == []


def test_compare_refuses_different_corpus():
    a = report(FP_BASE, {"t1": task_entry("passed", M_OK)})
    b = report(FP_BASE | {"eval_tasks_hash": "tasks-2"}, {"t1": task_entry("passed", M_OK)})
    with pytest.raises(EvalError):
        compare_reports(a, b)


def test_compare_corpus_block():
    a = report(FP_BASE, {"t1": task_entry("passed", M_OK), "t2": task_entry("failed", M_OK)})
    b = report(FP_BASE, {"t1": task_entry("passed", M_OK), "t2": task_entry("passed", M_OK)})
    out = compare_reports(a, b)
    assert out["corpus"]["tasks_total"] == 2
    assert out["corpus"]["tasks_passed"] == {"a": 1, "b": 2}
    assert out["corpus"]["task_pass_rate"] == {"a": 0.5, "b": 1.0}
