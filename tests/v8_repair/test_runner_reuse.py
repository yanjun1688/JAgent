"""P1b · C1：只重跑"键变了/新加"的步骤 + 它们的下游。

验收口径（brief §4 / D-027）：

* 5 步链：第 3 步失败，修复只改第 3 步 → 1/2 不重跑，3/4/5 重跑；
* 加步：新步骤 + 它的下游重跑，无关的已通过分支不重跑；
* 上游被修：上游的键变了，它的（传递）下游必须一起失效；
* 失败步即使键没变也是 root：它的（传递）下游同样失效；
* 不在失败步 / 变更步下游的 passed 步骤：artifact_refs / observations / status 原样保留。

工具调用次数是唯一可信的证据：状态可以复述，调用不会撒谎。
"""

from __future__ import annotations

from dataclasses import replace

from jagent.contracts import EventType, Plan, Step, Verify, VerifyOutcome
from jagent.harness.fold import RunState, StepState
from jagent.harness.repair import CallableRepairer
from tests.v8_repair.support import build_probe_app


def _step(
    step_id: str,
    value: int,
    *,
    depends_on: tuple[str, ...] = (),
    verify: str | None = None,
) -> Step:
    return Step(
        id=step_id,
        tool="probe",
        args={"value": value, "tag": step_id},
        depends_on=depends_on,
        verify=Verify(verify or f"value == {value}", needs=("value",)),
    )


def _tags(calls: list[dict]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for args in calls:
        out.setdefault(str(args["tag"]), []).append(int(args["value"]))
    return out


def _amended(app):
    return [event for event in app.store.read() if event.type is EventType.PLAN_AMENDED]


# --------------------------------------------------------------------------- #
# 1. 5 步链：修复只改第 3 步
# --------------------------------------------------------------------------- #

def test_five_step_chain_reruns_only_the_changed_step_and_its_downstream(tmp_path) -> None:
    plan = Plan(
        steps=(
            _step("s1", 1),
            _step("s2", 2, depends_on=("s1",)),
            # 工具会回显 3，但断言要求 33：第 3 步必然失败。
            _step("s3", 3, depends_on=("s2",), verify="value == 33"),
            _step("s4", 4, depends_on=("s3",)),
            _step("s5", 5, depends_on=("s4",)),
        )
    )

    def repair(*, plan: Plan, **_):
        steps = tuple(
            replace(step, args={**step.args, "value": 33}) if step.id == "s3" else step
            for step in plan.steps
        )
        return replace(plan, steps=steps)

    app, calls = build_probe_app(
        tmp_path / "app", repairer=CallableRepairer(lambda **kw: repair(**kw))
    )
    try:
        outcome = app.runner.start_and_run(plan)

        assert outcome.status == "completed", outcome.reason
        by_tag = _tags(calls)
        assert by_tag["s1"] == [1], "第 1 步不该重跑"
        assert by_tag["s2"] == [2], "第 2 步不该重跑"
        assert by_tag["s3"] == [3, 33], "失败步必须用新 args 重跑一次"
        assert by_tag["s4"] == [4], "下游在新计划下跑一次"
        assert by_tag["s5"] == [5], "下游在新计划下跑一次"

        run = app.world.runs[outcome.run_id]
        assert run.steps["s1"].attempts == 1
        assert run.steps["s2"].attempts == 1
        assert run.steps["s3"].attempts == 2
        assert run.steps["s1"].status == "passed"
        assert run.steps["s2"].status == "passed"

        amended = _amended(app)
        assert len(amended) == 1
        assert amended[0].payload["invalidated"] == ["s3", "s4", "s5"], (
            "plan.amended.invalidated 必须恰好是重跑的那几步"
        )
        assert amended[0].payload["plan"]["steps"][2]["args"]["value"] == 33
    finally:
        app.close()


def test_unchanged_passed_steps_keep_their_artifacts_and_observations(tmp_path) -> None:
    """键没变的已通过步骤：不重跑，也不清空产物 / 观测（复用不是"重新排队"）。"""

    plan = Plan(
        steps=(
            _step("s1", 1),
            _step("s2", 2, depends_on=("s1",)),
            _step("s3", 3, depends_on=("s2",), verify="value == 33"),
        )
    )

    def repair(*, plan: Plan, **_):
        steps = tuple(
            replace(step, args={**step.args, "value": 33}) if step.id == "s3" else step
            for step in plan.steps
        )
        return replace(plan, steps=steps)

    app, calls = build_probe_app(
        tmp_path / "app", repairer=CallableRepairer(lambda **kw: repair(**kw))
    )
    try:
        outcome = app.runner.start_and_run(plan)
        assert outcome.status == "completed", outcome.reason

        run = app.world.runs[outcome.run_id]
        s1_finished = [
            event
            for event in app.store.read()
            if event.type is EventType.STEP_FINISHED
            and event.payload["step_id"] == "s1"
        ]
        assert len(s1_finished) == 1, "键没变的 s1 不该再产生一次 step.finished"
        assert run.steps["s1"].artifact_refs == s1_finished[0].payload["artifact_refs"]
        assert run.steps["s1"].artifact_refs, "s1 的产物引用不能丢"
        assert run.steps["s1"].observations == s1_finished[0].payload["observations"]
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 2. 加步：新步骤 + 下游重跑，无关分支不重跑
# --------------------------------------------------------------------------- #

def test_added_step_reruns_only_itself_and_its_downstream(tmp_path) -> None:
    plan = Plan(
        steps=(
            _step("s1", 1),
            # 无关分支先跑完：修复后必须原样复用。
            _step("s4", 4, depends_on=("s1",)),
            _step("s5", 5, depends_on=("s4",)),
            # 失败分支
            _step("s2", 2, depends_on=("s1",), verify="value == 22"),
            _step("s3", 3, depends_on=("s2",)),
        )
    )

    def repair(*, plan: Plan, **_):
        by_id = plan.by_id()
        fixed_s2 = replace(by_id["s2"], args={**by_id["s2"].args, "value": 22})
        inserted = Step(
            id="s2b",
            tool="probe",
            args={"value": 22, "tag": "s2b"},
            depends_on=("s2",),
            verify=Verify("value == 22", needs=("value",)),
        )
        fixed_s3 = replace(by_id["s3"], depends_on=("s2b",))
        steps = tuple(
            {
                "s2": fixed_s2,
                "s3": fixed_s3,
            }.get(step.id, step)
            for step in plan.steps
        )
        return replace(plan, steps=(*steps, inserted))

    app, calls = build_probe_app(
        tmp_path / "app", repairer=CallableRepairer(lambda **kw: repair(**kw))
    )
    try:
        outcome = app.runner.start_and_run(plan)

        assert outcome.status == "completed", outcome.reason
        by_tag = _tags(calls)
        assert by_tag["s1"] == [1]
        assert by_tag["s4"] == [4], "无关分支的已通过步骤不该重跑"
        assert by_tag["s5"] == [5], "无关分支的已通过步骤不该重跑"
        assert by_tag["s2"] == [2, 22], "失败步被修好后重跑一次"
        assert by_tag["s2b"] == [22], "新步骤必须执行"
        assert by_tag["s3"] == [3], "新步骤的下游必须执行"

        amended = _amended(app)
        assert len(amended) == 1
        assert amended[0].payload["invalidated"] == ["s2", "s2b", "s3"]
        new_ids = [step["id"] for step in amended[0].payload["plan"]["steps"]]
        assert new_ids == ["s1", "s4", "s5", "s2", "s3", "s2b"], "plan.amended 必须载完整新计划"

        run = app.world.runs[outcome.run_id]
        assert run.steps["s4"].attempts == 1
        assert run.steps["s5"].attempts == 1

        # 加步后的完整计划也能从事件流原样重建（D-019），复用的分支保持 passed。
        app.reload()
        replayed = app.world.runs[outcome.run_id]
        assert replayed.status == "completed"
        assert replayed.plan == run.plan
        assert replayed.steps["s4"].status == "passed"
        assert replayed.steps["s4"].artifact_refs == run.steps["s4"].artifact_refs
        assert replayed.steps["s4"].observations == run.steps["s4"].observations
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 3. 上游被修：键变了，传递下游必须一起失效
# --------------------------------------------------------------------------- #

def test_repairing_an_upstream_step_invalidates_everything_downstream(tmp_path) -> None:
    plan = Plan(
        steps=(
            _step("s1", 1),
            _step("s2", 2, depends_on=("s1",), verify="value == 22"),
            _step("s3", 3, depends_on=("s2",)),
        )
    )

    def repair(*, plan: Plan, **_):
        by_id = plan.by_id()
        upstream = replace(by_id["s1"], args={**by_id["s1"].args, "note": "v2"})
        fixed = replace(by_id["s2"], args={**by_id["s2"].args, "value": 22})
        return replace(
            plan,
            steps=tuple({upstream.id: upstream, fixed.id: fixed}.get(step.id, step) for step in plan.steps),
        )

    app, calls = build_probe_app(
        tmp_path / "app", repairer=CallableRepairer(lambda **kw: repair(**kw))
    )
    try:
        outcome = app.runner.start_and_run(plan)

        assert outcome.status == "completed", outcome.reason
        by_tag = _tags(calls)
        assert by_tag["s1"] == [1, 1], "上游 args 变了，必须重跑"
        assert by_tag["s2"] == [2, 22]
        assert by_tag["s3"] == [3]

        amended = _amended(app)
        assert amended[0].payload["invalidated"] == ["s1", "s2", "s3"]
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 4. 失败步键没变：失败步仍是 root，新计划的传递下游必须一起失效（C-01）
# --------------------------------------------------------------------------- #

class _AlwaysPassVerifier:
    """只用于本用例：让重跑的 s1/s2 都能走完，从而数清 s2 是否真的重新执行。"""

    def verify(self, step, outcome) -> VerifyOutcome:
        return VerifyOutcome(passed=True, reason="")


def test_unchanged_key_failed_step_still_invalidates_its_downstream(tmp_path) -> None:
    """修复返回同一份计划（旧键 == 新键）时，失败步也必须按 root 处理。

    历史投影故意造出 ``s1=failed, s2=passed``（s2 依赖 s1）：这是"下游在旧计划里
    已通过、上游现在失败"的审计场景。失败步重跑可能产出不同产物，s2 不能继续复用。
    """

    plan = Plan(
        steps=(
            _step("s1", 1, verify="value == 2"),
            _step("s2", 2, depends_on=("s1",)),
        )
    )
    app, calls = build_probe_app(
        tmp_path / "app", repairer=CallableRepairer(lambda **_: plan)
    )
    try:
        run = RunState(
            run_id="run_failed_upstream",
            kind="execute",
            status="running",
            plan=plan,
            plan_hash=plan.hash(),
        )
        run.steps["s1"] = StepState(
            id="s1", status="failed", attempts=1,
            artifact_refs=["old-s1"], observations={"value": 1},
        )
        run.steps["s2"] = StepState(
            id="s2", status="passed", attempts=1,
            artifact_refs=["old-s2"], observations={"value": 2},
        )
        app.world.runs[run.run_id] = run

        # 旧计划 == 新计划：所有复用键都不变；唯一的重跑理由就是"失败步是 root"。
        assert app.runner._apply_repair(run.run_id, "s1", plan) is True

        amended = _amended(app)
        assert amended[0].payload["invalidated"] == ["s1", "s2"], (
            "失败步即使键没变，也必须把新计划里的传递下游一起失效"
        )
        assert run.steps["s1"].status == "pending"
        assert run.steps["s2"].status == "pending", "s2 不能继续停在 passed"

        # 真正跑一遍：s1 重跑、s2 也重新执行（不是复用旧产物）。
        app.runner.verifier = _AlwaysPassVerifier()
        outcome = app.runner.drive(run.run_id)

        assert outcome.status == "completed", outcome.reason
        assert [args["tag"] for args in calls] == ["s1", "s2"]
        assert run.steps["s1"].attempts == 2
        assert run.steps["s2"].attempts == 2, "s2 必须重新执行"
        assert run.steps["s2"].artifact_refs != ["old-s2"]
    finally:
        app.close()
