"""I1 · 终态 run 不可被任何后续事件改写。

规格依据：
- 宪法 I1：事件是真相（append-only；**终态不可改写**；状态是 fold）；
- 架构 §2 硬规定 3：所有转移显式化，非法转移直接拒绝（拒绝本身也落事件）；
- STATUS.md:58-59：终态 run 不可被任何事件改写（记录拒绝，丢弃原事件）。

复现：`docs/v8-wave2-bug2-01-repro.md`（主仓）。旧 writer 只拦 8 种 run 状态事件，
`step.finished` / `plan.amended` / `step.verified` / `artifact.written` 直接 append，
fold 无条件应用——一个 completed run 的 step 可以被事后改成 failed / pending，
且日志里没有拒绝记录、重放会忠实复现被污染的历史。
"""

from __future__ import annotations

import copy

import pytest

from jagent.contracts import EventDraft, EventType, Plan, Step, Verify, plan_to_dict
from jagent.harness.fold import fold
from jagent.harness.statemachine import IllegalTransitionError
from jagent.harness.store import EventStore

from .conftest import ARTIFACT_ID, RUN_ID, SESSION_ID, STEP_ID, Harness

#: 复现记录 seq 1–9 的事件形状；seq 10 是 run.completed，seq 11 才是迟到事件。
_PREFIX_TYPES = [
    EventType.RUN_STARTED,
    EventType.PLAN_FROZEN,
    EventType.STEP_STARTED,
    EventType.ARTIFACT_WRITTEN,
    EventType.STEP_FINISHED,
    EventType.STEP_VERIFIED,
    EventType.STEP_STATE_CHANGED,
    EventType.BUDGET_CONSUMED,
    EventType.REPORT_RENDERED,
]


# --------------------------------------------------------------------------- #
# 复现序列（复刻 docs/v8-wave2-bug2-01-repro.md §2.1 的 seq 1–10）
# --------------------------------------------------------------------------- #

def _seed_prefix(harness: Harness) -> tuple[Plan, dict]:
    """写到 `report.rendered` 为止（seq 1–9），返回 plan 与 result 快照。"""
    writer = harness.writer
    plan = Plan(steps=(Step(id=STEP_ID, tool="probe", verify=Verify("value == 'v1'")),))
    result = {"verdict": "completed"}

    writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=RUN_ID,
    )
    writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=RUN_ID,
    )
    writer.emit(
        EventType.STEP_STARTED,
        {"step_id": STEP_ID, "attempt": 1, "idempotency_key": "k1"},
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    writer.emit(
        EventType.ARTIFACT_WRITTEN,
        {"artifact_id": ARTIFACT_ID, "sha256": ARTIFACT_ID, "bytes": 14, "mime": "application/json"},
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    writer.emit(
        EventType.STEP_FINISHED,
        {
            "step_id": STEP_ID,
            "status": "ok",
            "artifact_refs": [ARTIFACT_ID],
            "observations": {"value": "v1"},
            "attempt": 1,
        },
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    writer.emit(
        EventType.STEP_VERIFIED,
        {
            "step_id": STEP_ID,
            "result": "pass",
            "checks": [{"expr": "value == 'v1'", "passed": True}],
            "reason": "",
        },
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    writer.step_to(RUN_ID, STEP_ID, "passed", "assertions_passed")
    writer.budget(RUN_ID, "steps")
    writer.emit(EventType.REPORT_RENDERED, {"result": result}, run_id=RUN_ID)
    return plan, result


def _seed_terminal(harness: Harness, *, outcome: str = "completed") -> None:
    _plan, result = _seed_prefix(harness)
    if outcome == "completed":
        harness.writer.emit(EventType.RUN_COMPLETED, {"result": result}, run_id=RUN_ID)
    elif outcome == "failed":
        harness.writer.emit(
            EventType.RUN_FAILED, {"reason": "seed_failed", "result": result}, run_id=RUN_ID
        )
    else:  # pragma: no cover - 参数保护
        raise ValueError(outcome)


def _snapshot(harness: Harness) -> dict:
    """终态前后必须逐字段一致的投影快照（fingerprint 覆盖关键状态）。"""
    run = harness.world.runs[RUN_ID]
    step = run.steps[STEP_ID]
    return copy.deepcopy(
        {
            "fingerprint": harness.world.fingerprint(),
            "run_status": run.status,
            "result": run.result,
            "plan_version": run.version,
            "step_status": step.status,
            "artifact_refs": list(step.artifact_refs),
            "evidence_seqs": list(step.evidence_seqs),
            "result_status": step.result_status,
            "checks": list(step.checks),
            "artifacts": harness.world.artifacts,
        }
    )


def _rejected(harness: Harness) -> list:
    return harness.of_type(EventType.RUN_TRANSITION_REJECTED)


# --------------------------------------------------------------------------- #
# 1. 复现的 11 步：completed 之后 step.finished 被拒绝，投影与产物引用不变
# --------------------------------------------------------------------------- #

def test_step_finished_after_completed_is_rejected_and_keeps_artifact_refs(harness) -> None:
    _seed_terminal(harness)
    before = _snapshot(harness)

    types = [event.type for event in harness.events()]
    assert types[:10] == [*_PREFIX_TYPES, EventType.RUN_COMPLETED]
    assert before["run_status"] == "completed"
    assert before["artifact_refs"] == [ARTIFACT_ID]

    # 迟到的 step.finished：旧 writer 会直接 append（复现 seq 11）。
    recorded = harness.writer.emit(
        EventType.STEP_FINISHED,
        {"step_id": STEP_ID, "status": "failed", "artifact_refs": [], "observations": {}},
        run_id=RUN_ID,
        step_id=STEP_ID,
    )

    # 写下的是一条拒绝事件，不是原事件；拒绝必须留痕，不能静默丢弃。
    assert recorded.type is EventType.RUN_TRANSITION_REJECTED
    assert recorded.run_id == RUN_ID
    assert recorded.step_id == STEP_ID
    payload = recorded.payload
    assert payload["rejected_type"] == "step.finished"
    assert payload["target_status"] == "completed"
    assert payload["from"] == "completed"
    assert payload["reason"]
    assert payload["illegal"] is True

    types = [event.type for event in harness.events()]
    assert types[10] is EventType.RUN_TRANSITION_REJECTED, "seq 11 已被替换成拒绝事件"
    assert len(harness.of_type(EventType.STEP_FINISHED)) == 1, "迟到的 step.finished 被丢弃"
    assert len(_rejected(harness)) == 1

    # 投影逐字段不变：artifact_refs 仍是原 sha，evidence_seqs 不再追加 11。
    after = _snapshot(harness)
    assert after == before
    assert harness.world.runs[RUN_ID].steps[STEP_ID].artifact_refs == [ARTIFACT_ID]
    assert harness.world.runs[RUN_ID].steps[STEP_ID].evidence_seqs == [5, 6]
    assert harness.world.runs[RUN_ID].steps[STEP_ID].result_status == "ok"


# --------------------------------------------------------------------------- #
# 2. completed 之后 plan.amended 被拒绝：passed 不能变回 pending
# --------------------------------------------------------------------------- #

def test_plan_amended_after_completed_cannot_reopen_a_passed_step(harness) -> None:
    _seed_terminal(harness)
    before = _snapshot(harness)

    recorded = harness.writer.emit(
        EventType.PLAN_AMENDED,
        {"steps": [], "reason": "late", "invalidated": [STEP_ID]},
        run_id=RUN_ID,
    )

    assert recorded.type is EventType.RUN_TRANSITION_REJECTED
    assert recorded.payload["rejected_type"] == "plan.amended"
    assert recorded.payload["target_status"] == "completed"
    assert harness.world.runs[RUN_ID].steps[STEP_ID].status == "passed"
    assert harness.world.runs[RUN_ID].version == before["plan_version"]
    assert harness.of_type(EventType.PLAN_AMENDED) == []
    assert _snapshot(harness) == before


# --------------------------------------------------------------------------- #
# 3. 守卫是通用的：step.verified / artifact.written / run.failed 都过同一入口
# --------------------------------------------------------------------------- #

def test_guard_covers_every_event_type_carrying_the_terminal_run_id(harness) -> None:
    _seed_terminal(harness)
    before = _snapshot(harness)

    late_events = [
        (EventType.STEP_VERIFIED, {"step_id": STEP_ID, "result": "fail", "checks": [], "reason": "late"}),
        (EventType.ARTIFACT_WRITTEN, {"artifact_id": "b" * 64, "sha256": "b" * 64, "bytes": 1, "mime": "text/plain"}),
        (EventType.RUN_FAILED, {"reason": "forged"}),
    ]
    rejected_types = []
    for type_, payload in late_events:
        recorded = harness.writer.emit(type_, payload, run_id=RUN_ID)
        assert recorded.type is EventType.RUN_TRANSITION_REJECTED
        assert recorded.payload["target_status"] == "completed"
        rejected_types.append(recorded.payload["rejected_type"])

    assert rejected_types == ["step.verified", "artifact.written", "run.failed"]
    assert len(_rejected(harness)) == len(late_events)
    # 迟到的 artifact.written 也不能往终态 run 里加产物。
    assert _snapshot(harness) == before
    assert list(harness.world.artifacts) == [ARTIFACT_ID]


# --------------------------------------------------------------------------- #
# 4. failed 也是终态（不只 completed）
# --------------------------------------------------------------------------- #

def test_failed_run_is_terminal_too(harness) -> None:
    _seed_terminal(harness, outcome="failed")
    before = _snapshot(harness)
    assert harness.world.runs[RUN_ID].status == "failed"

    recorded = harness.writer.emit(
        EventType.STEP_FINISHED,
        {"step_id": STEP_ID, "status": "failed", "artifact_refs": []},
        run_id=RUN_ID,
        step_id=STEP_ID,
    )

    assert recorded.type is EventType.RUN_TRANSITION_REJECTED
    assert recorded.payload["from"] == "failed"
    assert recorded.payload["target_status"] == "failed"
    assert _snapshot(harness) == before


# --------------------------------------------------------------------------- #
# 5. 不误伤：report.rendered 在 run.completed 之前仍必须落盘
# --------------------------------------------------------------------------- #

def test_report_rendered_before_run_completed_is_still_accepted(harness) -> None:
    _plan, result = _seed_prefix(harness)

    rendered = harness.of_type(EventType.REPORT_RENDERED)
    assert len(rendered) == 1, "report.rendered 必须能写在 run.completed 之前"
    assert rendered[0].seq == 9
    assert harness.world.runs[RUN_ID].result == result

    completed = harness.writer.emit(EventType.RUN_COMPLETED, {"result": result}, run_id=RUN_ID)
    assert completed.type is EventType.RUN_COMPLETED
    assert harness.world.runs[RUN_ID].status == "completed"
    assert _rejected(harness) == []


# --------------------------------------------------------------------------- #
# 6. 拒绝事件本身可重放、不递归；run_to 的拒绝照旧只有一条
# --------------------------------------------------------------------------- #

def test_transition_rejected_is_allowed_on_a_terminal_run_and_does_not_recurse(harness) -> None:
    _seed_terminal(harness)
    before = _snapshot(harness)

    recorded = harness.writer.emit(
        EventType.RUN_TRANSITION_REJECTED,
        {"from": "completed", "to": "failed", "reason": "manual", "illegal": True},
        run_id=RUN_ID,
    )

    assert recorded.type is EventType.RUN_TRANSITION_REJECTED
    assert recorded.payload["from"] == "completed"
    assert len(_rejected(harness)) == 1, "对拒绝事件不该再拒绝一次"
    assert _snapshot(harness) == before


def test_run_to_on_a_terminal_run_records_one_rejection_then_raises(harness) -> None:
    _seed_terminal(harness)
    before = _snapshot(harness)

    with pytest.raises(IllegalTransitionError):
        harness.writer.run_to(RUN_ID, "failed")

    rejected = _rejected(harness)
    assert len(rejected) == 1
    assert rejected[0].payload["to"] == "failed"
    assert _snapshot(harness) == before


# --------------------------------------------------------------------------- #
# 7. 没有 run_id 的事件不受影响
# --------------------------------------------------------------------------- #

def test_events_without_run_id_are_unaffected(harness) -> None:
    _seed_terminal(harness)
    before_count = len(harness.events())

    recorded = harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": "h"})

    assert recorded.type is EventType.TURN_RECEIVED
    assert recorded.run_id is None
    assert len(harness.events()) == before_count + 1
    assert _rejected(harness) == []


# --------------------------------------------------------------------------- #
# 8. 同一批里 [run.completed, step.finished]：第二条按落完后的终态判断
# --------------------------------------------------------------------------- #

def test_same_batch_terminal_then_step_event_is_rejected(harness) -> None:
    plan = Plan(steps=(Step(id=STEP_ID, tool="probe", verify=Verify("value == 'v1'")),))
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=RUN_ID,
    )
    harness.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=RUN_ID,
    )
    assert harness.world.runs[RUN_ID].status == "running"

    events = harness.writer.emit_many(
        [
            EventDraft(EventType.RUN_COMPLETED, {"result": {"verdict": "completed"}}, SESSION_ID, RUN_ID),
            EventDraft(
                EventType.STEP_FINISHED,
                {"step_id": STEP_ID, "status": "failed", "artifact_refs": []},
                SESSION_ID,
                RUN_ID,
                STEP_ID,
            ),
        ]
    )

    assert [event.type for event in events] == [
        EventType.RUN_COMPLETED,
        EventType.RUN_TRANSITION_REJECTED,
    ]
    assert events[1].payload["rejected_type"] == "step.finished"
    assert harness.world.runs[RUN_ID].steps[STEP_ID].result_status is None


# --------------------------------------------------------------------------- #
# 9. 重放两次一致；run.transition_rejected 可重放（不记 unknown event type）
# --------------------------------------------------------------------------- #

def test_replay_twice_is_identical_and_transition_rejected_is_known(harness) -> None:
    _seed_terminal(harness)
    harness.writer.emit(
        EventType.STEP_FINISHED,
        {"step_id": STEP_ID, "status": "failed", "artifact_refs": []},
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    harness.writer.emit(
        EventType.PLAN_AMENDED,
        {"steps": [], "reason": "late", "invalidated": [STEP_ID]},
        run_id=RUN_ID,
    )
    events = harness.store.read()

    first = fold(events)
    second = fold(events)

    assert first.fingerprint() == second.fingerprint()
    assert not [d for d in first.diagnostics if "unknown event type" in d], (
        "run.transition_rejected 必须可重放：未知事件类型会污染 diagnostics（BUG2-03）"
    )
    assert len([e for e in events if e.type is EventType.RUN_TRANSITION_REJECTED]) == 2
    assert first.runs[RUN_ID].status == "completed"
    assert first.runs[RUN_ID].steps[STEP_ID].status == "passed"
    assert first.runs[RUN_ID].steps[STEP_ID].artifact_refs == [ARTIFACT_ID]

    # 换一个 store 句柄（等价于进程重启）再重放，投影仍然一致。
    other = EventStore(harness.db_path)
    try:
        assert fold(other.read()).fingerprint() == first.fingerprint()
    finally:
        other.close()


# --------------------------------------------------------------------------- #
# 10. fold 双保险：重放“旧 writer 写下的”被污染历史日志，也必须保持未被改写
# --------------------------------------------------------------------------- #

def test_replay_ignores_late_events_in_a_historical_log(harness) -> None:
    _seed_terminal(harness)
    clean_events = harness.store.read()

    # 旧 writer 没有这条守卫：迟到事件会被直接 append。用 store 绕过新 writer
    # 造一份“历史日志”，验证 fold 自己也不接受终态后的改写。
    plan = Plan(steps=(Step(id=STEP_ID, tool="probe", verify=Verify("value == 'v1'")),))
    harness.store.append(
        [
            EventDraft(
                EventType.STEP_FINISHED,
                {"step_id": STEP_ID, "status": "failed", "artifact_refs": [], "observations": {}},
                SESSION_ID,
                RUN_ID,
                STEP_ID,
            ),
            EventDraft(
                EventType.PLAN_AMENDED,
                {
                    "plan": plan_to_dict(plan),
                    "plan_hash": plan.hash(),
                    "version": plan.version + 1,
                    "reason": "late",
                    "invalidated": [STEP_ID],
                },
                SESSION_ID,
                RUN_ID,
            ),
        ]
    )
    polluted_events = harness.store.read()
    assert len(polluted_events) == len(clean_events) + 2

    replayed = fold(polluted_events)

    assert replayed.fingerprint() == fold(clean_events).fingerprint()
    assert replayed.runs[RUN_ID].status == "completed"
    assert replayed.runs[RUN_ID].version == 1
    assert replayed.runs[RUN_ID].steps[STEP_ID].status == "passed"
    assert replayed.runs[RUN_ID].steps[STEP_ID].artifact_refs == [ARTIFACT_ID]
    assert replayed.runs[RUN_ID].steps[STEP_ID].evidence_seqs == [5, 6]
    # 不是静默跳过：诊断里能看出这两条被终态吸收了。
    ignored = [d for d in replayed.diagnostics if "ignored" in d]
    assert len(ignored) == 2
    assert not [d for d in replayed.diagnostics if "unknown event type" in d]
