"""I6 · 重放一致 + digest 链覆盖内容。

规格依据：
- §3.1「Event Log ... append-only；重放即恢复」；
- §3.3「同一事件流重放两次 → 状态一致、报告一致」；
- §3.3「只读 Event Log + Artifact Store，不调用任何模型」；
- store.py 模块注释「digest 覆盖上一条事件的 digest，重放两次必须得到同一个尾 digest」。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jagent.contracts import Event, EventType, Plan, Step, Verify
from jagent.harness.fold import fold
from jagent.harness.store import EventStore, TamperedHistoryError, replay_digest
from jagent.wiring import build_app


def _seed(harness, count: int = 20) -> None:
    for index in range(count):
        harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": f"h{index}"})


def _tamper_payload(db_path, seq: int, payload: dict) -> None:
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        conn.execute(
            "UPDATE events SET payload = ? WHERE seq = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), seq),
        )
        conn.commit()


def _raw_read(db_path) -> list[Event]:
    """绕过 store.read 的链校验，直接读原始行——只为构造"篡改后"的指纹。"""

    sql = (
        "SELECT seq, event_id, type, payload, session_id, run_id, step_id, at,"
        " schema_version, digest FROM events ORDER BY seq"
    )
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        rows = conn.execute(sql).fetchall()
    return [
        Event(
            event_id=r[1], seq=r[0], type=EventType(r[2]), payload=json.loads(r[3]),
            session_id=r[4], run_id=r[5], step_id=r[6], at=r[7],
            schema_version=r[8], digest=r[9],
        )
        for r in rows
    ]


def test_replay_digest_is_stable_across_two_reads(harness) -> None:
    _seed(harness)

    first = replay_digest(harness.store.read())
    second = replay_digest(harness.store.read())

    assert first == second
    assert first


def test_replay_fold_is_stable(harness) -> None:
    _seed(harness)
    events = harness.store.read()

    world_a = fold(events)
    world_b = fold(events)

    assert world_a.fingerprint() == world_b.fingerprint()


def test_replay_survives_process_reopen(harness) -> None:
    _seed(harness)
    before = replay_digest(harness.store.read())

    other = EventStore(harness.db_path)
    try:
        assert replay_digest(other.read()) == before
        assert other.max_seq() == harness.store.max_seq()
    finally:
        other.close()


def test_tampering_one_payload_changes_replay_digest(harness) -> None:
    _seed(harness)
    original = replay_digest(harness.store.read())

    _tamper_payload(harness.db_path, 5, {"text_hash": "forged-by-redteam"})
    tampered = replay_digest(_raw_read(harness.db_path))

    assert tampered != original, "digest 链没有覆盖 payload 内容"
    assert _raw_read(harness.db_path)[4].payload["text_hash"] == "forged-by-redteam"


def test_tampering_a_middle_event_breaks_the_chain(harness) -> None:
    _seed(harness)
    stored_digests = [event.digest for event in harness.store.read()]

    _tamper_payload(harness.db_path, 10, {"text_hash": "forged-middle"})
    tampered = _raw_read(harness.db_path)

    # 被篡改位置及其之后，重算出的链摘要都必须与存储值不同。
    assert replay_digest(tampered[:10]) != stored_digests[9]
    assert replay_digest(tampered) != stored_digests[-1]


def test_tampered_history_is_rejected_on_read(harness) -> None:
    """已修复（本轮）：EventStore.read 全量读时校验 digest 链，篡改直接拒绝。"""

    _seed(harness)
    _tamper_payload(harness.db_path, 5, {"text_hash": "forged"})

    with pytest.raises(TamperedHistoryError) as info:
        harness.store.read()

    assert info.value.seq == 5


def test_replay_uses_no_model(tmp_path) -> None:
    from jagent.agent.model import CountingModel, ScriptedModel

    class _Judge:
        """可返回 REJECT 的 judge 替身；重放测试里 judge 不该被调用。"""

        model = "fake-judge"

        def judge(self, request):
            return None

    model = CountingModel(ScriptedModel([]))
    app = build_app(tmp_path / "replay", model=model, judge=_Judge())
    try:
        assert model.calls == 0
        app.reload()
        assert model.calls == 0, "重放期间不允许任何模型调用"
    finally:
        app.close()


def test_report_evidence_points_to_real_events(app_factory) -> None:
    """I12 · 报告里的每个结论都能指回一条事件（§4.11）。"""

    app = app_factory("evidence")
    (app.root / "ws" / "notes.txt").write_text("hello", encoding="utf-8")
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "notes.txt"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        )
    )
    outcome = app.runner.start_and_run(plan)

    assert outcome.result is not None
    seqs = {event.seq for event in app.store.read()}
    for step in outcome.result.steps:
        assert step.evidence_seqs, f"{step.id} 没有证据 seq"
        assert set(step.evidence_seqs) <= seqs, "证据 seq 指向不存在的事件"


def test_crash_mid_step_is_reconciled_on_resume(harness) -> None:
    """E17「重放 + 对账 + 续跑」：in-doubt 步骤要按幂等键查一次到底做没做。"""

    from jagent.contracts import plan_to_dict

    from .conftest import make_runner, probe_spec

    spec = probe_spec("probe")
    calls: list[int] = []

    def fn(args, workspace, timeout):
        calls.append(1)
        from jagent.contracts import ToolResult

        return ToolResult(ok=True)

    plan = Plan(steps=(Step(id="s1", tool="probe", verify=Verify("ok", needs=("ok",))),))
    run_id = "run_crash"
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=run_id,
    )
    harness.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=run_id,
    )
    harness.writer.step_to(run_id, "s1", "running", "started")
    harness.writer.emit(
        EventType.STEP_STARTED,
        {"step_id": "s1", "attempt": 1, "idempotency_key": "key-1"},
        run_id=run_id,
        step_id="s1",
    )

    # 模拟进程重启：只从事件日志重建世界。
    harness.writer.world = fold(harness.store.read())
    runner = make_runner(harness, tools=[(spec, fn)])

    outcome = runner.resume(run_id)

    assert outcome.status == "completed", (
        f"in-doubt 步骤应被对账后继续（幂等键 key-1），实际 {outcome.status}: {outcome.reason}"
    )
    assert len(calls) <= 1


def test_replay_reproduces_the_completed_run(app_factory) -> None:
    app = app_factory("replay-run")
    (app.root / "ws" / "notes.txt").write_text("hello", encoding="utf-8")
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "notes.txt"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        )
    )
    outcome = app.runner.start_and_run(plan)
    assert outcome.status == "completed"
    before = app.world.runs[outcome.run_id]
    before_status, before_result = before.status, before.result

    app.reload()

    after = app.world.runs[outcome.run_id]
    assert after.status == before_status == "completed"
    assert after.result == before_result
    assert replay_digest(app.store.read())
