"""C3：``run.started.env`` 记录 git commit 与配置 hash；重放不读配置（宪法 I6）。

指纹只应来自"启动时算一次"，重放只应来自事件本身。这里的测试都围绕这两句。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import jagent.config as config_mod
from jagent.config import load_config
from jagent.contracts import EventDraft, EventType, Plan, validate_draft
from jagent.wiring import build_app

_COMMIT = re.compile(r"^(?:unknown|[0-9a-f]+(?:\+dirty)?)$")


@pytest.fixture(autouse=True)
def _clear_git_cache():
    """每个用例前后清掉 git 指纹缓存，避免跨用例串值。"""
    config_mod.git_commit.cache_clear()
    yield
    config_mod.git_commit.cache_clear()


def _started_events(app) -> list:
    return [event for event in app.store.read() if event.type is EventType.RUN_STARTED]


def test_run_started_records_git_and_config_fingerprint(tmp_path: Path) -> None:
    config = load_config()
    app = build_app(tmp_path / "app")
    try:
        app.runner.start_and_run(Plan(steps=()))
        events = _started_events(app)
    finally:
        app.close()

    assert len(events) == 1
    env = events[0].payload["env"]
    assert set(env) == {"git_commit", "config_hash"}
    assert env["config_hash"] == config.hash
    assert env["git_commit"] == config_mod.git_commit()
    assert _COMMIT.fullmatch(env["git_commit"])


def test_injected_config_hash_is_recorded(tmp_path: Path, config_copy: Path) -> None:
    budget = config_copy / "budget.toml"
    budget.write_text(
        budget.read_text(encoding="utf-8").replace("max_steps = 10", "max_steps = 11"),
        encoding="utf-8",
    )
    config = load_config(config_copy)
    app = build_app(tmp_path / "app", config=config)
    try:
        app.runner.start_and_run(Plan(steps=()))
        env = _started_events(app)[0].payload["env"]
    finally:
        app.close()

    assert config.budget.max_steps == 11
    assert env["config_hash"] == config.hash
    assert env["config_hash"] != load_config().hash


def test_run_started_requires_env() -> None:
    """env 是 run.started 的必填字段：手工事件少它就写不进去。"""
    with pytest.raises(ValueError, match="env"):
        validate_draft(
            EventDraft(EventType.RUN_STARTED, {"run_id": "run_x", "kind": "execute"}, "s_main")
        )


def test_git_commit_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(repo: Path, *args: str) -> str:
        calls.append(args)
        return "abc1234\n" if args[0] == "rev-parse" else ""

    monkeypatch.setattr(config_mod, "_git", fake)

    assert config_mod.git_commit() == "abc1234"
    assert config_mod.git_commit() == "abc1234"
    assert len(calls) == 2, "第一次探 rev-parse + status，第二次必须命中缓存"


def test_git_commit_marks_dirty_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(repo: Path, *args: str) -> str:
        return "abc1234\n" if args[0] == "rev-parse" else " M jagent/config.py\n"

    monkeypatch.setattr(config_mod, "_git", fake)

    assert config_mod.git_commit() == "abc1234+dirty"


def test_git_commit_without_git_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "_git", lambda repo, *args: None)

    assert config_mod.git_commit() == "unknown"


def test_git_commit_without_status_keeps_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """状态查不到时只报 commit：不把"不知道"伪装成干净或脏。"""

    def fake(repo: Path, *args: str) -> str | None:
        return "abc1234\n" if args[0] == "rev-parse" else None

    monkeypatch.setattr(config_mod, "_git", fake)

    assert config_mod.git_commit() == "abc1234"


def test_replay_reads_the_fingerprint_from_the_event(
    tmp_path: Path, config_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑完再改配置、断掉加载器，重放仍然拿到事件里那份指纹，且零配置读取。"""
    config = load_config(config_copy)
    app = build_app(tmp_path / "app", config=config)
    try:
        app.runner.start_and_run(Plan(steps=()))
        started = _started_events(app)[0]
        recorded = dict(started.payload["env"])

        # 改配置：如果重放去读文件，config_hash 会变。
        budget = config_copy / "budget.toml"
        budget.write_text(
            budget.read_text(encoding="utf-8").replace("max_steps = 10", "max_steps = 11"),
            encoding="utf-8",
        )
        before = app.store.read()

        loads: list[object] = []

        def boom(*args: object, **kwargs: object) -> None:
            loads.append(args)
            raise AssertionError("重放不许读配置")

        monkeypatch.setattr(config_mod, "load_config", boom)
        monkeypatch.setattr(config_mod, "git_commit", boom)

        world = app.reload()

        after = app.store.read()
        replayed = next(event for event in after if event.type is EventType.RUN_STARTED)
    finally:
        app.close()

    assert loads == []
    assert [event.seq for event in after] == [event.seq for event in before]
    assert replayed.payload["env"] == recorded
    assert world.runs[started.run_id].kind == "execute"
    assert recorded["config_hash"] == config.hash
