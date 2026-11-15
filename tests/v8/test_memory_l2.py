"""记忆 L2 · 外部记忆 memory.md（D-030：M5 四类 / M6-A 起草-确认 / M7-A 注入）。

契约：
- planner mode ``"memory"`` 起草条目（起草不是写入）→ ``memory.proposed``；
- 用户回复「确认」→ 写 ``<root>/memory/memory.md``（四类 section，条目带日期
  与来源轮次）+ ``memory.resolved{applied:true}``；回复「取消」→ 只落
  resolved{applied:false}，文件不动；
- 待确认状态随事件持久：进程重启后确认流仍然成立；
- 确认过的条目按行注入 router/planner（M7-A）——记忆只是背景，事实进建议
  必须由当前证据重新验证（policy 第 1 条守门）。
"""

from __future__ import annotations

import json

from jagent.contracts import (
    Budget,
    IntentDecision,
    IntentType,
    MemoryDraft,
    MemoryEntry,
    Plan,
    Step,
    Verify,
)
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter

from .test_memory_l1 import RecordingRouter


def _decision() -> IntentDecision:
    return IntentDecision(
        type=IntentType.EXECUTE, confidence=0.9, missing_params=(), reason="test"
    )


def _memory_app(tmp_path):
    """带 RecordingRouter 的装配：planner 首个 draft 由测试覆盖。"""
    router = RecordingRouter(_decision())
    app = build_app(tmp_path, router=router)
    return app, router


def test_proposed_entries_wait_for_confirmation(tmp_path) -> None:
    """Given planner 起草一条 user 记忆，When handle_turn，
    Then answered + 列出条目等确认；memory.proposed 落盘；文件还不存在。"""
    app, _router = _memory_app(tmp_path)
    app.session.planner = DraftPlanner(
        [MemoryDraft(entries=(MemoryEntry(category="user", content="投资风格保守"),))]
    )
    out = app.session.handle_turn("记住：我投资风格保守")

    assert out.status == "answered"
    assert "确认" in out.text and "投资风格保守" in out.text
    proposed = [e.payload for e in app.store.read() if e.type.value == "memory.proposed"]
    assert len(proposed) == 1
    assert proposed[0]["entries"][0] == {"category": "user", "content": "投资风格保守"}
    assert not (tmp_path / "memory" / "memory.md").exists(), "起草不是写入"


def test_confirmation_writes_memory_file_and_injects(tmp_path) -> None:
    """Given 一条待确认记忆，When 用户回复「确认」，Then 文件落盘
    （category section + 日期 + 来源轮次）、resolved 落盘；
    之后每轮 router 注入该条目（M7-A）。"""
    app, router = _memory_app(tmp_path)
    app.session.planner = DraftPlanner(
        [MemoryDraft(entries=(MemoryEntry(category="user", content="投资风格保守"),))]
    )
    app.session.handle_turn("记住：我投资风格保守")
    app.session.handle_turn("确认")

    path = tmp_path / "memory" / "memory.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "## user" in text
    assert "- [" in text and "投资风格保守" in text and "来源：第" in text
    resolved = [e.payload for e in app.store.read() if e.type.value == "memory.resolved"]
    assert resolved and resolved[-1]["applied"] is True and resolved[-1]["decision"] == "confirm"
    assert app.session.state.pending_memory == []

    # 注入（M7-A）：下一轮 router 收到 memory 行。
    (tmp_path / "ws" / "note.md").write_text("x\n", encoding="utf-8")
    app.session.handle_turn("随便看看 note.md")
    assert any("投资风格保守" in line for line in router.seen_memory[-1]), (
        "确认过的记忆没有注入 router"
    )


def test_rejection_discards_without_writing(tmp_path) -> None:
    """Given 待确认记忆，When 用户回复「取消」，Then resolved(applied:false)
    落盘、文件不动、待确认清空。"""
    app, _router = _memory_app(tmp_path)
    app.session.planner = DraftPlanner(
        [MemoryDraft(entries=(MemoryEntry(category="project", content="报表按周出"),))]
    )
    app.session.handle_turn("记住：报表按周出")
    app.session.handle_turn("取消")

    resolved = [e.payload for e in app.store.read() if e.type.value == "memory.resolved"]
    assert resolved[-1]["applied"] is False
    assert not (tmp_path / "memory" / "memory.md").exists()
    assert app.session.state.pending_memory == []


def test_pending_memory_survives_restart(tmp_path) -> None:
    """Given 起草后进程重启（同 events.db 重新装配），When 用户回复「确认」，
    Then 确认流仍然成立——待确认状态随事件持久，不靠内存。"""
    app = build_app(
        tmp_path,
        planner=DraftPlanner(
            [MemoryDraft(entries=(MemoryEntry(category="project", content="代号 v8"),))]
        ),
        router=FixedRouter(IntentType.EXECUTE),
    )
    app.session.handle_turn("记住：代号 v8")
    app.close()

    restarted = build_app(
        tmp_path,
        planner=DraftPlanner(
            [
                Plan(
                    steps=(
                        Step(
                            id="r",
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
    try:
        (tmp_path / "ws" / "note.md").write_text("x\n", encoding="utf-8")
        out = restarted.session.handle_turn("确认")
        assert out.status == "answered" and "已记下" in out.text
        assert (tmp_path / "memory" / "memory.md").exists()
    finally:
        restarted.close()


def test_planner_parses_memory_mode(tmp_path) -> None:
    """Given 模型输出 {"mode":"memory","entries":[...]}，When 解析，
    Then 得到 MemoryDraft（四类之外的 category 被拒）。"""
    from jagent.agent.llm_planner import LLMPlanner
    from jagent.harness.registry import build_default_registry

    from .test_llm_planner import FakeModel

    good = json.dumps(
        {
            "mode": "memory",
            "entries": [
                {"category": "user", "content": "偏好保守"},
                {"category": "nope", "content": "坏的"},
            ],
        }
    )
    model = FakeModel(good)
    planner = LLMPlanner(model, build_default_registry(tmp_path / "ws"))
    draft = planner.plan(decision=_decision(), slots={}, tools=[], text="记住我偏好保守")
    assert isinstance(draft, MemoryDraft)
    assert [entry.category for entry in draft.entries] == ["user"]
    assert "坏的" not in json.dumps([entry.content for entry in draft.entries])
