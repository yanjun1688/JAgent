"""P1a · B2/B3：runner 侧只喂失败事实，应用前用 check_plan 复核。

覆盖验收：

* 断言失败 + 模型返回修订计划 → 替换失败步、只重跑本步、run 继续到 completed；
* 非法计划（未知工具 / 缺 verify）→ check_plan 拒绝、不应用、不记修复次数、
  不重试、run 走既有失败路径；
* ModelUnavailable → None，行为与 P1a 之前的 HeuristicRepairer 一致；
* max_repairs=2 时第三次修复请求不会再调模型；
* 修复调用收到的 failure 只有有界的失败事实（observations / error_message）。
"""

from __future__ import annotations

import json

import pytest

from jagent.agent.llm_repair import LLMRepairer
from jagent.agent.model import ScriptedModel
from jagent.app.runner import REPAIR_ERROR_MESSAGE_CHARS, REPAIR_OBSERVATIONS_CHARS
from jagent.contracts import Budget, plan_from_dict, plan_to_dict
from jagent.harness.plancheck import check_plan
from jagent.harness.repair import HeuristicRepairer
from jagent.ports import ModelReply, ModelUnavailable
from tests.v8_repair.support import build_probe_app, fixed_plan, probe_plan


class _UnavailableModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0) -> ModelReply:
        self.calls += 1
        raise ModelUnavailable("offline")


def _amended_events(app) -> list:
    return [event for event in app.store.read() if event.type.value == "plan.amended"]


def _run_events(app, run_id: str) -> list:
    return [event for event in app.store.read() if event.run_id == run_id]


# --------------------------------------------------------------------------- #
# 1. 修订计划被应用：替换失败步、重跑、run 继续
# --------------------------------------------------------------------------- #

def test_amended_plan_replaces_the_failed_step_and_run_continues(tmp_path) -> None:
    fixed = fixed_plan()
    model = ScriptedModel([plan_to_dict(fixed)])
    app, calls = build_probe_app(tmp_path / "app", repairer=LLMRepairer(model))
    try:
        outcome = app.runner.start_and_run(probe_plan())

        assert outcome.status == "completed", outcome.reason
        run = app.world.runs[outcome.run_id]
        assert run.plan is not None
        assert run.plan.by_id()["s1"].args == {"value": 2}, "失败步没有被修订计划替换"
        assert run.steps["s1"].attempts == 2, "修订后只应重跑失败步一次"
        assert run.steps["s1"].status == "passed"
        assert run.version == 2

        amended = _amended_events(app)
        assert len(amended) == 1
        assert amended[0].payload["invalidated"] == ["s1"]
        # 落的是完整计划（不是只落变更的那一步）。
        assert amended[0].payload["plan"]["steps"][0]["args"] == {"value": 2}
        assert len(model.calls) == 1
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 2. 非法计划：check_plan 拒绝、不应用、不重试
# --------------------------------------------------------------------------- #

_UNKNOWN_TOOL = {
    "id": "s1",
    "tool": "ghost",
    "args": {},
    "depends_on": [],
    "verify": {"expr": "ok == true", "needs": ["ok"]},
}
_MISSING_VERIFY = {
    "id": "s1",
    "tool": "probe",
    "args": {"value": 2},
    "depends_on": [],
}


@pytest.mark.parametrize(
    ("bad_step", "expected_code"),
    [(_UNKNOWN_TOOL, "UNKNOWN_TOOL"), (_MISSING_VERIFY, "MISSING_VERIFY")],
)
def test_invalid_plan_is_rejected_before_apply_without_retry(
    tmp_path, bad_step: dict, expected_code: str
) -> None:
    model = ScriptedModel([{"steps": [bad_step]}])
    app, calls = build_probe_app(tmp_path / "app", repairer=LLMRepairer(model))
    try:
        outcome = app.runner.start_and_run(probe_plan())

        assert outcome.status == "failed"
        assert outcome.reason == "assertion:s1", "被拒绝的修复应走既有失败路径"
        assert len(model.calls) == 1, "复核不过不许重试"
        assert len(calls) == 1, "非法计划不许触发重跑"

        run = app.world.runs[outcome.run_id]
        assert run.plan is not None
        assert run.plan.by_id()["s1"].args == {"value": 1}, "非法计划不能被执行"
        assert run.steps["s1"].attempts == 1
        assert run.budget["repairs"] == 0, "复核不过不能记一次修复"
        assert not _amended_events(app)
        assert any(event.type.value == "run.failed" for event in _run_events(app, outcome.run_id))

        # 证明拒绝确实来自 check_plan，而不是别处的形状检查。
        specs = {name: app.registry.get(name) for name in app.registry.names()}
        codes = {
            issue.code
            for issue in check_plan(plan_from_dict({"steps": [bad_step]}), specs)
        }
        assert expected_code in codes
    finally:
        app.close()


class _MalformedRepairer:
    """端口返回了不是 Plan 的东西：复核层必须 fail-closed，不能落事件或记预算。"""

    def repair(self, *, plan, step_id, failure, attempt):
        return {"steps": []}


def test_non_plan_repair_output_is_never_applied(tmp_path) -> None:
    app, calls = build_probe_app(tmp_path / "app", repairer=_MalformedRepairer())
    try:
        outcome = app.runner.start_and_run(probe_plan())

        assert outcome.status == "failed"
        assert outcome.reason == "assertion:s1"
        assert len(calls) == 1
        run = app.world.runs[outcome.run_id]
        assert run.budget["repairs"] == 0
        assert not _amended_events(app)
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 3. ModelUnavailable：None，行为与 P1a 之前一致
# --------------------------------------------------------------------------- #

def test_model_unavailable_keeps_the_pre_p1_failure_path(tmp_path) -> None:
    plan = probe_plan()
    model = _UnavailableModel()
    app, calls = build_probe_app(tmp_path / "app", repairer=LLMRepairer(model))
    try:
        outcome = app.runner.start_and_run(plan)

        assert outcome.status == "failed"
        assert outcome.reason == "assertion:s1"
        assert model.calls == 1, "模型不可用只调一次，不重试"
        assert len(calls) == 1
        run = app.world.runs[outcome.run_id]
        assert run.budget["repairs"] == 0
        assert not _amended_events(app)
    finally:
        app.close()

    # P1a 之前：断言失败 + 启发式修复器 = 同样的 run.failed，没有 plan.amended。
    baseline, baseline_calls = build_probe_app(
        tmp_path / "baseline", repairer=HeuristicRepairer()
    )
    try:
        expected = baseline.runner.start_and_run(plan)
        assert expected.status == outcome.status
        assert expected.reason == outcome.reason
        assert len(baseline_calls) == 1
        assert not _amended_events(baseline)
    finally:
        baseline.close()


# --------------------------------------------------------------------------- #
# 4. 修复预算：max_repairs=2 → 最多两次模型调用
# --------------------------------------------------------------------------- #

def test_third_repair_request_does_not_call_the_model(tmp_path) -> None:
    plan = probe_plan()
    # 模型两次都给"同样会失败"的计划：修复会被应用，但 run 仍然失败。
    model = ScriptedModel([plan_to_dict(plan), plan_to_dict(plan)])
    app, calls = build_probe_app(
        tmp_path / "app", repairer=LLMRepairer(model), budget=Budget(max_repairs=2)
    )
    try:
        outcome = app.runner.start_and_run(plan)

        assert outcome.status == "failed"
        assert len(model.calls) == 2, "第三次修复请求必须被预算拦住，不能调模型"
        run = app.world.runs[outcome.run_id]
        assert run.budget["repairs"] == 2
        assert run.steps["s1"].attempts == 3, "初次 + 两次修复重跑"
        assert len(calls) == 3
        assert len(_amended_events(app)) == 2
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 5. 只喂有界失败事实
# --------------------------------------------------------------------------- #

class _RecordingRepairer:
    def __init__(self) -> None:
        self.failure: dict | None = None
        self.step_id = ""
        self.attempt = 0

    def repair(self, *, plan, step_id, failure, attempt):
        self.failure = dict(failure)
        self.step_id = step_id
        self.attempt = attempt
        return None


def test_repair_call_receives_only_bounded_failure_facts(tmp_path) -> None:
    recorder = _RecordingRepairer()
    app, calls = build_probe_app(
        tmp_path / "app", repairer=recorder, stderr="boom" + "s" * 5000
    )
    try:
        outcome = app.runner.start_and_run(probe_plan())

        assert outcome.status == "failed"
        assert len(calls) == 1
        assert recorder.failure is not None
        failure = recorder.failure
        assert set(failure) == {
            "class",
            "reason",
            "error_code",
            "checks",
            "observations",
            "error_message",
        }, "修复调用只能多带 observations / error_message，不能塞全量历史"

        assert failure["class"] == "assertion"
        assert failure["error_code"] is None
        assert "value == 2" in failure["reason"]
        assert failure["checks"]

        observations = failure["observations"]
        assert isinstance(observations, dict)
        assert observations["value"] == "1"
        assert observations["stderr"].startswith("boom")
        summary = json.dumps(observations, ensure_ascii=False, separators=(",", ":"))
        assert len(summary) <= REPAIR_OBSERVATIONS_CHARS
        assert observations["stderr"].endswith("（已截断）")

        assert isinstance(failure["error_message"], str)
        assert failure["error_message"].startswith("boom")
        assert len(failure["error_message"]) <= REPAIR_ERROR_MESSAGE_CHARS
        assert failure["error_message"].endswith("（已截断）")
        assert recorder.step_id == "s1"
        assert recorder.attempt == 1
    finally:
        app.close()
