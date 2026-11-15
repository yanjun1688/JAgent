"""任务集 schema 校验（fail-closed）：未知 kind / 未知参数 / 缺字段一律拒跑。"""

from __future__ import annotations

import hashlib

import pytest

from jagent.evaluation import EVAL_SCHEMA, EvalError, load_tasks, tasks_from_mapping


def base_data(**task_overrides) -> dict:
    task = {
        "description": "读文件并汇报",
        "turns": ["读取 notes/a.txt"],
        "checks": [{"kind": "run_verdict", "expect": "completed"}],
    }
    task.update(task_overrides)
    return {"schema": EVAL_SCHEMA, "task": {"t1": task}}


# --------------------------------------------------------------------------- #
# 合法路径
# --------------------------------------------------------------------------- #


def test_valid_corpus_loads_with_defaults():
    corpus = tasks_from_mapping(base_data(), tasks_hash="h")
    assert len(corpus.tasks) == 1
    task = corpus.tasks[0]
    assert task.task_id == "t1"
    assert task.description == "读文件并汇报"
    assert task.k == 1  # 默认 k=1
    assert task.tags == ()
    assert task.workspace == {}
    assert task.turns == ({"text": "读取 notes/a.txt"},)
    assert task.checks[0].kind == "run_verdict"
    assert corpus.tasks_hash == "h"


def test_task_fields_round_trip():
    data = base_data(
        model="fs_read",
        k=2,
        tags=["execute", "happy_path"],
        workspace={"notes/a.txt": "内容"},
        turns=["第一句", {"action": "cancel"}, {"text": "notes/a.txt", "answers": {"target": "notes/a.txt"}}],
        checks=[
            {"kind": "run_verdict", "expect": "cancelled", "at": "last"},
            {"kind": "event_count", "type": "run.cancelled", "exact": 1},
        ],
    )
    task = tasks_from_mapping(data, tasks_hash="h").tasks[0]
    assert task.k == 2
    assert task.tags == ("execute", "happy_path")
    assert task.workspace == {"notes/a.txt": "内容"}
    assert task.model == "fs_read"
    assert task.turns == (
        {"text": "第一句"},
        {"action": "cancel"},
        {"text": "notes/a.txt", "answers": {"target": "notes/a.txt"}},
    )
    assert [c.kind for c in task.checks] == ["run_verdict", "event_count"]


def test_model_defaults_and_validation():
    task = tasks_from_mapping(base_data(), tasks_hash="h").tasks[0]
    assert task.model == ""  # 缺省空串：runner 用 "default" 脚本
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(model=""), tasks_hash="h")
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(model=123), tasks_hash="h")


def test_load_tasks_hashes_file_bytes(tmp_path):
    path = tmp_path / "tasks.toml"
    path.write_text(
        f'schema = "{EVAL_SCHEMA}"\n[task.t1]\ndescription = "x"\nturns = ["hi"]\nchecks = []\n',
        encoding="utf-8",
    )
    # checks 为空这里必然被拒——只为验证 hash 先构造合法内容
    path.write_text(
        f'schema = "{EVAL_SCHEMA}"\n[task.t1]\ndescription = "x"\nturns = ["hi"]\n'
        'checks = [{kind = "turn_outcome", expect = "answered"}]\n',
        encoding="utf-8",
    )
    corpus = load_tasks(path)
    assert corpus.tasks_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert corpus.tasks[0].task_id == "t1"


def test_load_tasks_missing_file_refuses(tmp_path):
    with pytest.raises(EvalError):
        load_tasks(tmp_path / "nope.toml")


# --------------------------------------------------------------------------- #
# 顶层与任务字段
# --------------------------------------------------------------------------- #


def test_missing_or_wrong_schema_refuses():
    with pytest.raises(EvalError):
        tasks_from_mapping({"task": {"t1": base_data()["task"]["t1"]}}, tasks_hash="h")
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data() | {"schema": "eval.tasks.v0"}, tasks_hash="h")


def test_unknown_top_level_key_refuses():
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data() | {"meta": {"x": 1}}, tasks_hash="h")


def test_empty_corpus_refuses():
    with pytest.raises(EvalError):
        tasks_from_mapping({"schema": EVAL_SCHEMA, "task": {}}, tasks_hash="h")


def test_task_missing_description_refuses():
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(description=None), tasks_hash="h")
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(description=""), tasks_hash="h")


@pytest.mark.parametrize(
    "turns",
    [
        None,
        [],
        "hi",
        [123],
        [{"action": "bogus"}],
        [{"action": "cancel", "text": "x"}],
        [{"text": "x", "bogus": 1}],
        [{"text": "x", "answers": "no"}],
        [{"text": "x", "answers": {1: "v"}}],
        [{"text": ""}],
    ],
)
def test_bad_turns_refuse(turns):
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(turns=turns), tasks_hash="h")


def test_text_turn_with_or_without_answers():
    task = tasks_from_mapping(
        base_data(turns=["第一句", {"text": "第二句"}]), tasks_hash="h"
    ).tasks[0]
    assert task.turns == (
        {"text": "第一句"},
        {"text": "第二句", "answers": {}},  # 归一化：text turn 统一带 answers 键
    )


@pytest.mark.parametrize("checks", [None, [], [{"kind": "nope"}], [{}], "run_verdict"])
def test_bad_checks_refuse(checks):
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(checks=checks), tasks_hash="h")


@pytest.mark.parametrize("k", [0, -1, "2", 1.5])
def test_bad_k_refuses(k):
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(k=k), tasks_hash="h")


def test_unknown_task_key_refuses():
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(bogus=1), tasks_hash="h")


@pytest.mark.parametrize("ws", [{"a.txt": 1}, {"/abs/x.txt": "v"}, {"../up.txt": "v"}])
def test_bad_workspace_refuses(ws):
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(workspace=ws), tasks_hash="h")


# --------------------------------------------------------------------------- #
# checks 参数校验（每个 kind 的必填 / 可选 / 值域）
# --------------------------------------------------------------------------- #


def test_unknown_kind_refuses():
    with pytest.raises(EvalError):
        tasks_from_mapping(
            base_data(checks=[{"kind": "star_rating", "stars": 5}]), tasks_hash="h"
        )


def test_run_verdict_param_validation():
    bad = [
        {"kind": "run_verdict"},  # 缺 expect
        {"kind": "run_verdict", "expect": "great"},  # 值域外
        {"kind": "run_verdict", "expect": "completed", "at": "first"},  # at 值域外
        {"kind": "run_verdict", "expect": "completed", "runs": 1},  # 未知参数
    ]
    for check in bad:
        with pytest.raises(EvalError):
            tasks_from_mapping(base_data(checks=[check]), tasks_hash="h")


def test_turn_outcome_param_validation():
    for check in [{"kind": "turn_outcome"}, {"kind": "turn_outcome", "expect": "answered", "at": "middle"}]:
        with pytest.raises(EvalError):
            tasks_from_mapping(base_data(checks=[check]), tasks_hash="h")


def test_steps_passed_min_param_validation():
    for count in ["2", -1, None]:
        with pytest.raises(EvalError):
            tasks_from_mapping(
                base_data(checks=[{"kind": "steps_passed_min", "count": count}]),
                tasks_hash="h",
            )


def test_budget_within_needs_at_least_one_bound():
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(checks=[{"kind": "budget_within"}]), tasks_hash="h")
    with pytest.raises(EvalError):
        tasks_from_mapping(
            base_data(checks=[{"kind": "budget_within", "tokens_in": -1}]), tasks_hash="h"
        )


@pytest.mark.parametrize("kind", ["approvals", "clarify_asked"])
def test_count_kinds_need_min_or_max(kind):
    with pytest.raises(EvalError):
        tasks_from_mapping(base_data(checks=[{"kind": kind}]), tasks_hash="h")


def test_event_count_param_validation():
    bad = [
        {"kind": "event_count"},  # 缺 min/max/exact
        {"kind": "event_count", "type": "not.an.event", "min": 1},  # 未知事件
        {"kind": "event_count", "type": "run.cancelled", "min": -1},
        {"kind": "event_count", "min": 1},  # 缺 type
    ]
    for check in bad:
        with pytest.raises(EvalError):
            tasks_from_mapping(base_data(checks=[check]), tasks_hash="h")


def test_gate_checked_param_validation():
    bad = [
        {"kind": "gate_checked", "pass": "yes"},
        {"kind": "gate_checked", "judge": "pass"},  # judge 没有 PASS（I3）
        {"kind": "gate_checked", "rules_present": "advice.no_citation"},  # 要列表
        {"kind": "gate_checked", "bogus": 1},
    ]
    for check in bad:
        with pytest.raises(EvalError):
            tasks_from_mapping(base_data(checks=[check]), tasks_hash="h")


def test_advice_delivered_param_validation():
    bad = [
        {"kind": "advice_delivered", "mode": 1},
        {"kind": "advice_delivered", "fallback": "no"},
        {"kind": "advice_delivered", "min_citations": -2},
    ]
    for check in bad:
        with pytest.raises(EvalError):
            tasks_from_mapping(base_data(checks=[check]), tasks_hash="h")


def test_replay_consistent_takes_no_params():
    with pytest.raises(EvalError):
        tasks_from_mapping(
            base_data(checks=[{"kind": "replay_consistent", "extra": 1}]), tasks_hash="h"
        )


def test_step_started_max_param_validation():
    bad = [
        {"kind": "step_started_max"},  # 缺 step_id / max
        {"kind": "step_started_max", "step_id": "s1"},  # 缺 max
        {"kind": "step_started_max", "max": 1},  # 缺 step_id
        {"kind": "step_started_max", "step_id": "", "max": 1},
        {"kind": "step_started_max", "step_id": "s1", "max": -1},
        {"kind": "step_started_max", "step_id": "s1", "max": 1, "extra": 2},
    ]
    for check in bad:
        with pytest.raises(EvalError):
            tasks_from_mapping(base_data(checks=[check]), tasks_hash="h")
