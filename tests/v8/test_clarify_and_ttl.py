"""P2-d/e · clarify 槽位跨重启 + TTL 过期自动取消（BUG2-04 / BUG2-08 余项）。

BUG2-04（P1）：planner 现场澄清的槽位白名单只活在内存（``_dynamic_slots``），
进程重启后用户对**已持久化问题**的回答被静默丢弃（红队 RT-H2 的反面——
丢弃答案同样是用户可见的伤害）。修复：``CLARIFY_ASKED`` 事件里本来就有
``questions[].slot``，fold 记进 SessionState 重放即恢复；答案白名单取并集。

BUG2-08 余项：TTL 过期只让 resolve 返回 False，run 永久停在 waiting_human。
修复：每个 turn 边界扫一次过期审批，走 cancel_run 的 waiting_human 分支
（D-028 语义：run.cancelled + token 作废 + reply 通知）——确定性，无后台线程。
"""

from __future__ import annotations

import time

from jagent.contracts import (
    ClarifyDraft,
    EventType,
    IntentType,
    Plan,
    Question,
    Step,
    Verify,
    plan_to_dict,
)
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter


def test_clarify_answer_survives_process_restart(tmp_path) -> None:
    """Given turn 1 planner 现场澄清（slot=file_name）后进程退出，
    When 重启（同 events.db 重新装配、全新 Session）后 turn 2 用户回答该槽位，
    Then 回答被接受（slot.updated 落盘）并正常执行——而不是被白名单静默丢弃。
    注意必须真重启：reload 只重建 world，Session 的内存白名单还活着。"""
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "note.md"},
                verify=Verify("ok", needs=("ok",)),
            ),
        ),
    )
    app = build_app(
        tmp_path,
        planner=DraftPlanner(
            [
                ClarifyDraft(questions=(Question(slot="file_name", text="读哪个文件？"),)),
                plan,
            ]
        ),
        router=FixedRouter(IntentType.EXECUTE),
    )
    (app.root / "ws" / "note.md").write_text("hello\n", encoding="utf-8")

    first = app.session.handle_turn("帮我读一下文件")
    assert first.status == "needs_input", f"前置失败：{first.status}"
    app.close()

    # 进程重启：同一 events.db 重新装配，全新 Session（内存白名单为空）。
    restarted = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.EXECUTE),
    )
    try:
        second = restarted.session.handle_turn("帮我读一下文件", {"file_name": "note.md"})
        assert second.status == "answered", (
            f"重启后回答没被接受：{second.status} {second.text[:80]!r}"
        )
        slot_events = [
            e.payload
            for e in restarted.store.read()
            if e.type is EventType.SLOT_UPDATED and e.payload.get("name") == "file_name"
        ]
        assert slot_events, "重启后用户回答被静默丢弃（BUG2-04）"
        assert slot_events[-1]["value"] == "note.md"
    finally:
        restarted.close()


def test_expired_approval_run_is_cancelled_on_next_turn(tmp_path) -> None:
    """Given 一个审批已过 TTL 的挂起 run，When 下一个 turn 开始，
    Then 该 run 被自动取消（reason=ttl_expired、token 作废、reply 通知落盘），
    当前 turn 本身正常进行——确定性扫描，不依赖后台线程。"""
    app = build_app(tmp_path)
    plan = Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "x.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
            ),
        ),
    )
    run_id = "run-ttl-expired"
    writer = app.writer
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
    writer.emit(
        EventType.APPROVAL_REQUESTED,
        {
            "step_id": "write",
            "token": "tok_ttl",
            "payload_hash": "h" * 64,
            "expires_at": time.time() - 30.0,  # 已过期
        },
        run_id=run_id,
        step_id="write",
    )
    writer.emit(EventType.RUN_SUSPENDED, {"at_step": "write"}, run_id=run_id, step_id="write")
    assert app.world.runs[run_id].status == "waiting_human"

    app.session.handle_turn("你好")

    # turn 本身的走向（模板模式可能反问路径）不是被测行为；要验证的是：
    # turn 开始时的过期扫描把挂起 run 收走了。
    turns = [e for e in app.store.read() if e.type is EventType.TURN_RECEIVED]
    assert turns, "handle_turn 正常执行"
    run = app.world.runs[run_id]
    assert run.status == "cancelled"
    assert run.cancel_reason == "ttl_expired"
    assert run.approval is None, "token 已作废"
    cancelled = [e for e in app.store.read() if e.type is EventType.RUN_CANCELLED]
    assert len(cancelled) == 1 and cancelled[0].payload["reason"] == "ttl_expired"
    # D-028 的 U2 收尾照旧：会话流里有 reply 通知
    delivered = [e.payload for e in app.store.read() if e.type is EventType.ADVICE_DELIVERED]
    assert any("已取消" in str((d.get("advice") or {}).get("body")) for d in delivered)
