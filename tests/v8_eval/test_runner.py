"""评估 runner（scripts/run_eval.py）：替身分派、端到端驱动、确定性、manifest。

runner 是离线工具面（D-035）：经 build_app 生产装配 + 确定性替身模型驱动，
从事件流评分。这里同时是语料自检——真实 tasks.toml 必须能被 runner 跑绿。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from jagent.config import CONFIG_ROOT, config_manifest
from jagent.evaluation import EvalError, load_tasks

_REPO = Path(__file__).resolve().parents[2]
_CORPUS = CONFIG_ROOT / "eval" / "tasks.toml"

#: runner 是本地工具（scripts/ 按仓库约定不入库）——本文件只在带 scripts/ 的工作区
#: 运行；公开快照上跳过，投影核心（evaluation.py）的测试不受影响。
pytestmark = pytest.mark.skipif(
    not (_REPO / "scripts" / "run_eval.py").is_file(),
    reason="scripts/run_eval.py 不在公开仓库发布（D-034 re-baseline 约定）",
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_eval", _REPO / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


@pytest.fixture(scope="module")
def corpus():
    return load_tasks(_CORPUS)


# ------------------------------------------------------------------ 替身分派 --


def test_purpose_model_dispatches_by_system_anchor(runner):
    model = runner.PurposeModel(
        {
            "router": ['{"type": "execute", "confidence": 0.9, "reason": "r"}'],
            "judge": ["NONE"],
        }
    )
    reply = model.complete(system="你是 JAgent 的意图路由器。", prompt="x")
    assert reply.text.startswith('{"type"')
    assert model.complete(system="你是 JAgent 的建议合规评审（judge）。", prompt="x").text == "NONE"
    assert model.calls == ["router", "judge"]


def test_purpose_model_repeats_last_on_exhaustion(runner):
    model = runner.PurposeModel({"router": ["a", "b"]})
    assert model.complete(system="你是 JAgent 的意图路由器。", prompt="").text == "a"
    assert model.complete(system="你是 JAgent 的意图路由器。", prompt="").text == "b"
    assert model.complete(system="你是 JAgent 的意图路由器。", prompt="").text == "b"


def test_purpose_model_converts_dict_to_json(runner):
    model = runner.PurposeModel({"planner": [{"mode": "reply", "text": "ok"}]})
    reply = model.complete(system="你是 JAgent 的计划器。", prompt="")
    assert '"mode"' in reply.text and "reply" in reply.text


def test_purpose_model_unknown_purpose_fails(runner):
    model = runner.PurposeModel({})
    from jagent.agent.model import ModelCallFailed

    with pytest.raises(ModelCallFailed):
        model.complete(system="毫不相干的系统提示", prompt="")


# ------------------------------------------------------------------ 端到端 --


def test_unknown_script_refuses(runner):
    data = {
        "schema": "eval.tasks.v1",
        "task": {
            "t": {
                "description": "d",
                "model": "no_such_script",
                "turns": ["hi"],
                "checks": [{"kind": "turn_outcome", "expect": "answered"}],
            }
        },
    }
    from jagent.evaluation import tasks_from_mapping

    mini = tasks_from_mapping(data, tasks_hash="h")
    with pytest.raises(EvalError):
        runner.run_corpus(mini)


def test_mini_corpus_end_to_end(runner, tmp_path):
    """最小语料端到端：驱动真实 build_app（替身模型），从事件流评分出报告。"""
    data = {
        "schema": "eval.tasks.v1",
        "task": {
            "mini_read": {
                "description": "读文件",
                "model": "fs_read",
                "tags": ["execute"],
                "workspace": {"doc.md": "# 标题\n正文。\n"},
                "turns": ["读取 doc.md"],
                "checks": [
                    {"kind": "run_verdict", "expect": "completed"},
                    {"kind": "verify_consistent"},
                ],
            },
        },
    }
    from jagent.evaluation import tasks_from_mapping

    mini = tasks_from_mapping(data, tasks_hash="h" * 2)
    report = runner.run_corpus(mini)
    entry = report["tasks"]["mini_read"]
    assert entry["verdict"] == "passed"
    assert entry["attempts"][0]["verdict"] == "pass"
    assert report["corpus"]["tasks_passed"] == 1
    fp = report["fingerprint"]
    assert fp["git_commit"] and fp["config_hash"] and fp["eval_tasks_hash"] == "h" * 2
    assert "config/eval/tasks.toml" in fp["config_files"]
    assert "gate/policy.md" in fp["config_files"]


def test_real_corpus_runs_green(runner, corpus):
    """语料自检：真实 tasks.toml 的 16 个任务全部 pass^k 通过。

    这条同时钉住 runner 与语料的契约——谁坏了这里都会红。
    """
    report = runner.run_corpus(corpus)
    failed = {tid: e["verdict"] for tid, e in report["tasks"].items() if e["verdict"] != "passed"}
    assert not failed, f"语料自检失败：{failed}"


def test_report_verdicts_deterministic(runner, corpus):
    """同一语料跑两遍：verdict 与任务集指纹一致（物理量浮动属预期，§6）。"""
    a = runner.run_corpus(corpus)
    b = runner.run_corpus(corpus)
    assert a["fingerprint"]["eval_tasks_hash"] == b["fingerprint"]["eval_tasks_hash"]
    assert {
        tid: e["verdict"] for tid, e in a["tasks"].items()
    } == {
        tid: e["verdict"] for tid, e in b["tasks"].items()
    }


# ------------------------------------------------------------------ manifest --


def test_config_manifest_covers_config_and_policy():
    manifest = config_manifest()
    assert "config/budget.toml" in manifest
    assert "config/eval/tasks.toml" in manifest
    assert "config/prompts/router.md" in manifest
    assert "gate/policy.md" in manifest
    assert manifest == config_manifest()  # 稳定


# ---------------------------------------------------------------- 渲染与落盘 --


def _mini_corpus():
    from jagent.evaluation import tasks_from_mapping

    data = {
        "schema": "eval.tasks.v1",
        "task": {
            "mini_read": {
                "description": "读文件",
                "model": "fs_read",
                "workspace": {"doc.md": "# 标题\n正文。\n"},
                "turns": ["读取 doc.md"],
                "checks": [{"kind": "run_verdict", "expect": "completed"}],
            },
        },
    }
    return tasks_from_mapping(data, tasks_hash="h" * 3)


def test_render_report_md_lists_tasks(runner):
    report = runner.run_corpus(_mini_corpus())
    md = runner.render_report_md(report)
    assert "mini_read" in md and "passed" in md
    assert "config_hash" in md


def test_render_compare_md_lists_changes(runner):
    from jagent.evaluation import compare_reports

    a = runner.run_corpus(_mini_corpus())
    b = runner.run_corpus(_mini_corpus())
    md = runner.render_compare_md(compare_reports(a, b))
    assert "对照报告" in md and "mini_read" in md


def test_write_report_creates_json_and_md(runner, tmp_path):
    report = runner.run_corpus(_mini_corpus())
    out_dir = runner._write_report(report, tmp_path)
    assert (out_dir / "report.json").is_file()
    assert (out_dir / "report.md").is_file()
    import json

    loaded = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert loaded["tasks"]["mini_read"]["verdict"] == "passed"
