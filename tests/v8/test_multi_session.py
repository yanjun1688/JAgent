"""多对话 · 会话隔离与生命周期（用户裁决：要改，需要多对话）。

口径（D-034）：
- **每个对话一个会话**：``POST /api/sessions`` 创建（``s_<hex>``），首条消息落
  事件前不出现在列表（fold 惰性建态）；``/chat`` 接受可选 ``session_id``，
  缺省 ``s_main``（向后兼容：既有调用方/测试零改动）；
- **每会话一套执行组件**（Writer/Executor/Human/Runner/Session，共享判定与
  生成组件 + store/artifacts/registry）——事件按所属会话正确打戳，
  history/slots/pending_memory 天然隔离；
- **长期记忆 memory.md 全局共享**（D-030 本义：跨会话）；
- **并发双会话的投影一致性属产品化阶段**（store 落盘有锁；串行使用安全）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jagent.api import create_app
from jagent.contracts import (
    Budget,
    IntentType,
    MemoryDraft,
    MemoryEntry,
    Plan,
    ReplyDraft,
    Step,
    Verify,
)
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


def test_two_sessions_have_isolated_histories_and_correct_stamps(tmp_path) -> None:
    """Given 两个会话各聊各的，When 读事件与各自投影，
    Then 事件按所属会话打戳、history/slots 互不可见——A 的名字不进 B 的历史。"""
    app = build_app(
        tmp_path,
        planner=DraftPlanner(
            [ReplyDraft(text="好的。"), ReplyDraft(text="好的。"), ReplyDraft(text="好的。")]
        ),
        router=FixedRouter(IntentType.EXECUTE),
    )
    try:
        sid_a = app.create_session()
        sid_b = app.create_session()

        app.session_for(sid_a).handle_turn("我叫张三，做保守投资的。")
        app.session_for(sid_b).handle_turn("B 的消息")

        # 事件打戳：A/B 的事件各归各的 session_id
        stamps = {e.session_id for e in app.store.read() if e.session_id in (sid_a, sid_b)}
        assert stamps == {sid_a, sid_b}

        # 历史隔离：A 有张三，B 没有
        hist_a = "\n".join(h["text"] for h in app.session_for(sid_a).state.history)
        hist_b = "\n".join(h["text"] for h in app.session_for(sid_b).state.history)
        assert "张三" in hist_a
        assert "张三" not in hist_b

        # 会话各自的 runs 列表互不串
        app.session_for(sid_b).handle_turn("写 done.txt")  # 会触发 run（模板模式写不了？用 read 兜底）
        runs_b = app.session_for(sid_b).state.runs
        runs_a = app.session_for(sid_a).state.runs
        assert runs_a == [] or all(r not in runs_b for r in runs_a)
    finally:
        app.close()


def test_runs_are_stamped_with_their_session(tmp_path) -> None:
    """Given 会话 B 里跑一个 run，When 读事件，
    Then RUN_STARTED 及后续 run 级事件的 session_id 是 B——
    run 永远归属触发它的对话。"""
    app = build_app(
        tmp_path,
        planner=DraftPlanner(
            [
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
            ]
        ),
        router=FixedRouter(IntentType.EXECUTE),
        budget=Budget(auto_approve_tools=("fs.read",)),
    )
    (tmp_path / "ws" / "note.md").write_text("x\n", encoding="utf-8")
    sid_b = app.create_session()
    out = app.session_for(sid_b).handle_turn("读 note.md")

    assert out.status == "answered"
    run_events = [e for e in app.store.read() if e.run_id == out.run_id]
    assert run_events, "run 事件存在"
    assert {e.session_id for e in run_events} == {sid_b}, "run 事件必须归属触发它的会话"
    assert out.run_id in app.session_for(sid_b).state.runs
    try:
        app.close()
    finally:
        pass


def test_sessions_api_lifecycle_and_default_compat(tmp_path) -> None:
    """Given API 面，When 创建/列出/带 session_id 对话/不带 session_id 对话，
    Then 创建返回新 id；列表含元数据；缺省走 s_main（向后兼容）。"""
    api = create_app(root=tmp_path)
    with TestClient(api) as client:
        app_obj = client.app.state.jagent
        (tmp_path / "ws" / "note.md").write_text("x\n", encoding="utf-8")

        created = client.post("/api/sessions")
        assert created.status_code == 200
        sid = created.json()["session_id"]
        assert sid != "s_main"

        # 带 session_id 对话：确认归属
        chat = client.post("/chat", json={"text": "读一下 note.md", "session_id": sid})
        assert chat.status_code == 200
        assert chat.json()["session_id"] == sid

        # 缺省对话：走 s_main（向后兼容）
        default_chat = client.post("/chat", json={"text": "写 done.txt 内容 hi"})
        assert default_chat.status_code == 200
        assert default_chat.json()["session_id"] == "s_main"

        listing = client.get("/api/sessions")
        assert listing.status_code == 200
        sessions = {item["session_id"]: item for item in listing.json()["sessions"]}
        assert sid in sessions and "s_main" in sessions
        assert sessions[sid]["turns"] >= 1
        assert sessions[sid]["title"], "列表应给标题（首轮 user 文本截断）"

        # 重启：两条会话都还在（事件持久）
        app_obj.close()
    restarted = create_app(root=tmp_path)
    with TestClient(restarted) as client2:
        listing2 = client2.get("/api/sessions")
        ids = {item["session_id"] for item in listing2.json()["sessions"]}
        assert sid in ids and "s_main" in ids, "重启后会话列表完整"
        api2 = client2.app
        api2.state.jagent.close()


def test_memory_pending_is_per_session(tmp_path) -> None:
    """Given 会话 A 有待确认记忆，When 会话 B 的用户回复「确认」，
    Then B 不消费 A 的待确认（各会话的 pending_memory 隔离）——
    长期记忆文件 memory.md 是全局共享的，但**待确认队列**是每会话的。"""
    app = build_app(
        tmp_path,
        planner=DraftPlanner(
            [
                MemoryDraft(entries=(MemoryEntry(category="user", content="偏好保守"),)),
                ReplyDraft(text="B 正常回答。"),
            ]
        ),
        router=FixedRouter(IntentType.EXECUTE),
    )
    try:
        sid_a = app.create_session()
        app.session_for(sid_a).handle_turn("记住：我投资风格保守")
        assert app.session_for(sid_a).state.pending_memory

        sid_b = app.create_session()
        out_b = app.session_for(sid_b).handle_turn("确认")

        assert app.session_for(sid_a).state.pending_memory, "A 的待确认不该被 B 消费"
        resolved = [
            e.payload for e in app.store.read() if e.type.value == "memory.resolved"
        ]
        assert resolved == [], "B 的「确认」没有可确认的 pending，不落 resolved"
        assert "已记下" not in out_b.text
    finally:
        app.close()
