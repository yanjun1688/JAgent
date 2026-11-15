"""S3+S4 · 取消入口、幂等、token 作废与 U2 收尾（#17 / D-028）。

规格（用户裁决后的实现口径，D-028 记录）：
- **U2 收尾**：取消的 run 没有结论——不合成建议（Propose 不调用）、
  不落 fallback、``run_aborted`` 枚举删除（D-002 预留值不可达即删，D-011 先例）；
  会话流以 reply 通知收尾（``advice.delivered{mode:"reply"}``），与 chat 场景
  「reply 模式也要留痕」的既有性质一致；
- **入口**：``Session.cancel_run`` + ``POST /api/runs/{id}/cancel``；
- **幂等**：重复取消返回当前态（200），不落第二条事件；
  completed/failed 的 run 取消 → RuntimeError（入口 409）；
- **token 作废**：waiting_human 中取消后，旧 token 的 approve 被拒；
- **边界取消**：running 的 run 通过 runner 标记在步边界消费（S2），
  cancel 入口本身不写事件。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jagent.api import create_app
from jagent.contracts import (
    Advice,
    Budget,
    Citation,
    EventType,
    Idempotency,
    IntentType,
    Plan,
    SideEffect,
    Step,
    ToolResult,
    ToolSpec,
    Verify,
)
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter
from tests.v8_acceptance.support import FakePropose, write_doc


# --------------------------------------------------------------------------- #
# 会话级夹具：带一个 gated 写步骤的计划 → handle_turn 停在 waiting_human
# --------------------------------------------------------------------------- #

def _gated_write_plan() -> Plan:
    return Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "x.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
                gated=True,
            ),
        ),
    )


def _waiting_human_app(tmp_path: Path):
    app = build_app(
        tmp_path,
        planner=DraftPlanner([_gated_write_plan()]),
        router=FixedRouter(IntentType.EXECUTE),
    )
    outcome = app.session.handle_turn("写一个 x.txt")
    assert outcome.status == "waiting_human", f"前置失败：{outcome.status}"
    return app, outcome.run_id


# --------------------------------------------------------------------------- #
# S3 场景 1：waiting_human 中取消 → run.cancelled + reply 通知，token 作废
# --------------------------------------------------------------------------- #

def test_cancel_waiting_human_run_emits_cancel_and_reply_notice(tmp_path) -> None:
    """Given 一个挂起等人的 run，When cancel_run，
    Then 恰好一条 run.cancelled{reason} + 一条 reply 通知；
    不写 turn.ended（原 turn 已以 handed_off 结束）；
    旧 token 再来 approve 被拒。"""
    app, run_id = _waiting_human_app(tmp_path)
    turns_before = len(app.store.read())

    result = app.session.cancel_run(run_id, "user cancelled")

    assert result.status == "answered"
    assert "已取消" in result.text
    events = app.store.read()
    cancelled = [e for e in events if e.type is EventType.RUN_CANCELLED]
    assert len(cancelled) == 1
    assert cancelled[0].payload["reason"] == "user cancelled"

    delivered = [e.payload for e in events if e.type is EventType.ADVICE_DELIVERED]
    assert len(delivered) == 1, "取消收尾恰好一条 reply 通知"
    assert delivered[0]["mode"] == "reply"
    assert "已取消" in delivered[0]["advice"]["body"]

    # 通知走 reply 侧合规闸（profile=reply），不是建议闸（不产 mode=gated/grounded）
    checked = [e.payload for e in events if e.type is EventType.GATE_CHECKED]
    assert checked and all(c.get("profile") == "reply" for c in checked)

    # run 终态；取消之后没有新 turn 事件（取消不是 turn）
    assert app.world.runs[run_id].status == "cancelled"
    turn_events_after = [
        e for e in events[turns_before:] if e.type in (EventType.TURN_RECEIVED, EventType.TURN_ENDED)
    ]
    assert turn_events_after == []

    # token 作废：旧审批再来 approve → 拒绝
    with pytest.raises(RuntimeError):
        app.session.resume_run(run_id, "approve")


# --------------------------------------------------------------------------- #
# S3 场景 2：重复取消幂等——返回当前态，事件不翻倍
# --------------------------------------------------------------------------- #

def test_cancel_twice_is_idempotent(tmp_path) -> None:
    """Given 已取消的 run，When 再次 cancel_run，
    Then 返回同样的通知文本，run.cancelled / advice.delivered 仍各只有一条。"""
    app, run_id = _waiting_human_app(tmp_path)
    first = app.session.cancel_run(run_id, "user cancelled")

    second = app.session.cancel_run(run_id, "impotent second")

    assert second.status == "answered"
    assert second.text == first.text
    events = app.store.read()
    assert len([e for e in events if e.type is EventType.RUN_CANCELLED]) == 1
    assert len([e for e in events if e.type is EventType.ADVICE_DELIVERED]) == 1


# --------------------------------------------------------------------------- #
# S3 场景 3：终态 completed/failed 的 run 不能取消；未知 run 报 KeyError
# --------------------------------------------------------------------------- #

def test_cancel_completed_run_raises(tmp_path) -> None:
    """Given 一个已 completed 的 run，When cancel_run，Then RuntimeError。"""
    plan = Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "done.txt", "content": "ok"},
                verify=Verify("ok", needs=("ok",)),
            ),
        ),
    )
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.EXECUTE),
        budget=Budget(auto_approve_tools=("fs.write",)),
    )
    outcome = app.session.handle_turn("写 done.txt")
    assert outcome.status == "answered" and outcome.result is not None

    with pytest.raises(RuntimeError):
        app.session.cancel_run(outcome.run_id, "late cancel")


def test_cancel_unknown_run_raises(tmp_path) -> None:
    """Given 不存在的 run_id，When cancel_run，Then KeyError。"""
    app = build_app(tmp_path)
    with pytest.raises(KeyError):
        app.session.cancel_run("run_missing", "nope")


# --------------------------------------------------------------------------- #
# S4 场景 4：取消的 gather run 不合成——Propose 零调用，无建议闸事件
# --------------------------------------------------------------------------- #

def test_cancelled_gather_run_never_composes(tmp_path) -> None:
    """Given gather 意图 + then=advice 的 run 停在 waiting_human，When 取消，
    Then Propose 零调用：没有 grounded/insufficient_evidence/fallback 形状的
    交付，唯一交付是 reply 通知（U2：不合成，run_aborted 不存在）。"""
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
                gated=True,
            ),
        ),
        then="advice",
    )
    advice = Advice(body="文件存在。", refs=(Citation("read", "content"),))
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.GATHER),
        propose=FakePropose(advice),
    )
    write_doc(app)
    outcome = app.session.handle_turn("先看文件再判断")
    assert outcome.status == "waiting_human"

    app.session.cancel_run(outcome.run_id, "user cancelled")

    assert app.session.propose is not None
    assert _propose_calls(app) == 0, "取消的 run 不合成（U2 / D-028）"
    delivered = [
        e.payload for e in app.store.read() if e.type is EventType.ADVICE_DELIVERED
    ]
    assert len(delivered) == 1
    assert delivered[0]["mode"] == "reply"


# --------------------------------------------------------------------------- #
# S4 场景 5：turn 进行中取消（步边界消费）→ 原 turn 以通知收尾，不合成
# --------------------------------------------------------------------------- #

def test_cancel_mid_turn_answers_with_notice_and_skips_compose(tmp_path) -> None:
    """Given 一个执行期请求取消的探针工具，When handle_turn 跑完，
    Then turn 以 reply 通知收尾（status=answered），Propose 零调用，
    run.cancelled 在事件流里。"""
    plan = Plan(
        steps=(
            Step(id="probe", tool="probe_cancel", verify=Verify("True", needs=())),
        ),
        then="advice",
    )
    advice = Advice(body="不应被合成。")
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan]),
        router=FixedRouter(IntentType.EXECUTE),
        propose=FakePropose(advice),
    )

    def cancelling_fn(args, workspace, timeout):
        # 工具只知道"当前这个 run"：run.started 已落盘，投影里有且只有它。
        run_id = next(iter(app.world.runs))
        app.session.runner.cancel(run_id, "mid-turn cancel")
        return ToolResult(ok=True)

    app.registry.add(
        ToolSpec(
            name="probe_cancel",
            version="1",
            args_schema={"type": "object", "properties": {}},
            side_effect=SideEffect.NONE,
            idempotency=Idempotency.NATURAL,
            readonly=True,
            observations=("ok",),
            timeout_s=5.0,
        ),
        cancelling_fn,
    )

    outcome = app.session.handle_turn("跑一下探针")

    assert outcome.status == "answered"
    assert "已取消" in outcome.text
    events = app.store.read()
    assert len([e for e in events if e.type is EventType.RUN_CANCELLED]) == 1
    delivered = [e.payload for e in events if e.type is EventType.ADVICE_DELIVERED]
    assert len(delivered) == 1 and delivered[0]["mode"] == "reply"
    assert app.session.propose is not None and _propose_calls(app) == 0


# --------------------------------------------------------------------------- #
# S3 场景 6：HTTP 入口——200 / 幂等 200 / 终态 409 / 未知 404 / 旧 token 409
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(tmp_path: Path):
    api = create_app(root=tmp_path)
    with TestClient(api) as test_client:
        yield test_client
    api.state.jagent.close()


def test_cancel_http_endpoint_lifecycle(client: TestClient, tmp_path: Path) -> None:
    """Given 一个 completed 的只读 run 和一个 waiting_human 的 gated run，
    When 依次打取消端点，Then completed → 409；waiting_human → 200 且幂等；
    取消后旧 token 的 approve → 409；未知 run → 404。"""
    app_obj = client.app.state.jagent
    ws = app_obj.runner.workspace
    (Path(ws) / "note.md").write_text("hello\n", encoding="utf-8")

    completed = app_obj.runner.start_and_run(
        Plan(
            steps=(
                Step(
                    id="read",
                    tool="fs.read",
                    args={"path": "note.md"},
                    verify=Verify("ok", needs=("ok",)),
                ),
            ),
        )
    )
    assert completed.status == "completed"
    late = client.post(f"/runs/{completed.run_id}/cancel", json={"reason": "late"})
    assert late.status_code == 409

    gated = app_obj.runner.start_and_run(
        Plan(
            steps=(
                Step(
                    id="write",
                    tool="fs.write",
                    args={"path": "x.txt", "content": "hi"},
                    verify=Verify("ok", needs=("ok",)),
                    gated=True,
                ),
            ),
        )
    )
    assert gated.status == "waiting_human"

    first = client.post(f"/runs/{gated.run_id}/cancel", json={"reason": "user cancelled"})
    assert first.status_code == 200
    again = client.post(f"/runs/{gated.run_id}/cancel", json={"reason": "second"})
    assert again.status_code == 200, "重复取消幂等，不 409"

    stale = client.post(f"/runs/{gated.run_id}/decision", json={"decision": "approve"})
    assert stale.status_code == 409, "取消后旧 token 的 approve 被拒"

    missing = client.post("/runs/run_missing/cancel", json={"reason": "x"})
    assert missing.status_code == 404

    app_obj.close()


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #

def _propose_calls(app) -> int:
    propose = app.session.propose
    return len(getattr(propose, "calls", []))
