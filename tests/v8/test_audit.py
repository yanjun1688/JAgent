"""D-032 · 运行验证视图（用户需求：LLM 说"完成了"必须可对账）。

会话里模型说"完成了"不算数——``audit.run_verification`` 把那次任务的事件链
整理成「声称 vs 证据」并列视图：每步工具/参数/观测/断言/终态/失效，
加全部模型调用与一致性结论。纯派生、零新存储（数据本来就在 events.db）。
"""

from __future__ import annotations

from jagent.audit import run_verification
from jagent.contracts import Budget, EventType, IntentType, Plan, ReplyDraft, Step, Verify, plan_to_dict
from jagent.harness.fold import World
from jagent.harness.store import EventStore
from jagent.harness.writer import Writer
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter


def _write_plan() -> Plan:
    return Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "done.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
            ),
        ),
    )


def test_completed_run_claims_match_evidence(tmp_path) -> None:
    """Given 一个真实完成的 run，When 核对，
    Then consistent=true：声称 completed 且每步 passed + 断言 pass，
    每步的工具/参数/观测都在视图里可查。"""
    app = build_app(
        tmp_path,
        planner=DraftPlanner([_write_plan(), ReplyDraft(text="写好了。")]),
        router=FixedRouter(IntentType.EXECUTE),
        budget=Budget(auto_approve_tools=("fs.write",)),
    )
    try:
        out = app.session.handle_turn("写 done.txt")
        assert out.status == "answered"
        run_id = out.run_id
        events = app.store.read()
        view = run_verification(events, run_id)

        assert view["claimed"]["verdict"] == "completed"
        assert view["consistent"] is True, view["problems"]
        assert len(view["steps"]) == 1
        step = view["steps"][0]
        assert step["tool"] == "fs.write"
        assert step["args"] == {"path": "done.txt", "content": "hi"}
        assert step["status"] == "passed"
        assert step["verified"] == "pass"
        assert step["observations"]["ok"] is True
        assert step["attempts"] == 1
    finally:
        app.close()


def test_failed_run_exposes_the_failure_evidence(tmp_path) -> None:
    """Given 一个断言失败的 run（文件内容只有 4 字节却断言 >100），
    When 核对，Then consistent=false 的声称是 failed、失败步骤的分类与
    error_code 可查——失败原因可追溯，不是一句"没成功"。"""
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 100", needs=("content",)),
            ),
        ),
    )
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.EXECUTE),
    )
    (app.root / "ws" / "doc.md").write_text("短内容", encoding="utf-8")
    try:
        app.session.handle_turn("读 doc.md")
        run_id = next(iter(app.world.runs))
        view = run_verification(app.store.read(), run_id)

        assert view["claimed"]["verdict"] == "failed"
        assert view["claimed"]["fail_reason"], "失败必须有可追溯原因"
        step = view["steps"][0]
        assert step["status"] == "failed"
        assert step["verified"] == "fail"
        assert step["failure"] is not None and step["failure"]["class"] == "assertion"
        assert step["observations"]["content"], "失败步骤的观测也要可查"
    finally:
        app.close()


def test_verification_follows_the_amended_plan(tmp_path) -> None:
    """修复（plan.amended）改了步骤参数后，验证视图必须显示**实际执行的**
    amended 参数——证据视图跟随最新计划，否则审计失真（review 发现）。

    注意时序：amend 必须发生在 run 终态之前（I1 终态守卫会吸收迟到的
    plan.amended——那不是失真，是守卫）。手工种子事件按真实修复时序排列。
    """
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        ),
    )
    amended = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "note.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        ),
    )
    writer = Writer(EventStore(tmp_path / "events.db"), World(), "s-audit")
    try:
        run_id = "run-amended"
        writer.emit(
            EventType.RUN_STARTED,
            {"run_id": run_id, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
            run_id=run_id,
        )
        writer.emit(
            EventType.PLAN_FROZEN,
            {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
            run_id=run_id,
        )
        # 修复发生在 run **运行中**（I1 允许；终态后才 amend 会被守卫吸收）。
        writer.emit(
            EventType.PLAN_AMENDED,
            {
                "plan": plan_to_dict(amended),
                "plan_hash": amended.hash(),
                "version": 2,
                "reason": "repair",
                "invalidated": [],
            },
            run_id=run_id,
        )
        writer.emit(
            EventType.STEP_STARTED,
            {"step_id": "read", "attempt": 1, "idempotency_key": "k1"},
            run_id=run_id, step_id="read",
        )
        writer.emit(
            EventType.STEP_FINISHED,
            {
                "step_id": "read",
                "status": "ok",
                "artifact_refs": [],
                "observations": {"content": "world", "ok": True},
                "attempt": 1,
            },
            run_id=run_id, step_id="read",
        )
        writer.emit(
            EventType.STEP_VERIFIED,
            {
                "step_id": "read",
                "result": "pass",
                "checks": [{"expr": "len(content) > 0", "passed": True}],
                "reason": "",
            },
            run_id=run_id, step_id="read",
        )
        writer.step_to(run_id, "read", "passed", "assertions_passed")
        writer.budget(run_id, "steps")
        result = {"verdict": "completed", "summary": "完成"}
        writer.emit(EventType.REPORT_RENDERED, {"result": result}, run_id=run_id)
        writer.emit(EventType.RUN_COMPLETED, {"result": result}, run_id=run_id)

        view = run_verification(writer.store.read(), run_id)

        assert view["steps"][0]["args"]["path"] == "note.md", (
            "证据视图必须跟随实际执行的最新计划（amended），而不是冻结时的旧参数"
        )
    finally:
        writer.store.close()


def test_unknown_run_returns_empty(tmp_path) -> None:
    """Given 不存在的 run_id，When 核对，Then 空视图（调用方自行 404）。"""
    app = build_app(tmp_path)
    try:
        assert run_verification(app.store.read(), "run_missing") == {}
    finally:
        app.close()
