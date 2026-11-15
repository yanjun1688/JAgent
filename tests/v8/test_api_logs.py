"""JAgent v8 · /api/logs 日志出口测试（logsink 管道）。

日志是运维观测（uvicorn 访问线、服务生命周期、模型调用、异常堆栈），**不是
事件**：零写事件库、零落盘，进程重启即清空，只留一个"当前控制台"窗口。

这里钉住四件事：
1. 环形缓冲的游标语义（monotonic id + since(after) + 满员丢最老）；
2. /api/logs 端点契约（{logs, next}，坏游标友好拒绝）；
3. 服务生命周期落日志（chat 轮次、未捕获异常带堆栈）；
4. 敏感值（审批 token）永远不进日志——只有失守才会出现的信号。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jagent.agent.planner import ScriptedPlanner
from jagent.api import create_app
from jagent.api.logsink import LogSink, LogSinkHandler, attach
from jagent.contracts import Plan, Step, Verify
from jagent.wiring import build_app
from tests.support import FixedRouter


# --------------------------------------------------------------------------- #
# LogSink 单元：环形缓冲 + 游标
# --------------------------------------------------------------------------- #


def test_sink_ids_are_monotonic_and_since_filters() -> None:
    sink = LogSink(capacity=10)
    first = sink.append(level="INFO", logger="t", message="first")
    second = sink.append(level="INFO", logger="t", message="second")

    all_entries, cursor = sink.since(-1)
    assert [entry.id for entry in all_entries] == [first, second]
    assert cursor == second

    tail, cursor = sink.since(first)
    assert [entry.message for entry in tail] == ["second"]
    assert cursor == second


def test_sink_ring_evicts_oldest() -> None:
    sink = LogSink(capacity=3)
    for index in range(5):
        sink.append(level="INFO", logger="t", message=f"m{index}")

    entries, cursor = sink.since(-1)
    assert [entry.message for entry in entries] == ["m2", "m3", "m4"]
    assert cursor == entries[-1].id


def test_sink_is_thread_safe() -> None:
    sink = LogSink(capacity=1000)
    workers = 8
    per_worker = 100

    def worker() -> None:
        for _ in range(per_worker):
            sink.append(level="INFO", logger="t", message="x")

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries, _ = sink.since(-1)
    assert len(entries) == workers * per_worker
    assert len({entry.id for entry in entries}) == workers * per_worker


def test_handler_carries_level_logger_and_stack() -> None:
    sink = LogSink()
    handler = LogSinkHandler(sink)
    logger = logging.getLogger("logsink.test.handler")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.warning("careful: %s", "detail")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("turn failed")
    finally:
        logger.removeHandler(handler)

    entries, _ = sink.since(-1)
    assert [entry.level for entry in entries] == ["WARNING", "ERROR"]
    assert entries[0].logger == "logsink.test.handler"
    assert "careful: detail" in entries[0].message
    assert "turn failed" in entries[1].message
    assert "RuntimeError: boom" in entries[1].message


def test_attach_streams_logging_into_sink() -> None:
    sink = LogSink()
    detach = attach(sink)
    try:
        logging.getLogger("logsink.test.attach").info("hello sink")
        logging.getLogger("logsink.test.attach").debug("quiet")
    finally:
        detach()

    messages = [entry.message for entry in sink.since(-1)[0]]
    assert any("hello sink" in message for message in messages)
    # INFO 门槛：debug 不进窗口（"详细"是运维视角，不是全量转储）。
    assert all("quiet" not in message for message in messages)


# --------------------------------------------------------------------------- #
# /api/logs 端点
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path: Path):
    api = create_app(root=tmp_path)
    with TestClient(api) as test_client:
        yield test_client
    api.state.jagent.close()


def test_api_logs_empty_window(client: TestClient) -> None:
    response = client.get("/api/logs")

    assert response.status_code == 200
    body = response.json()
    assert body["logs"] == []
    assert body["next"] == -1


def test_api_logs_cursor_increment(client: TestClient) -> None:
    sink = client.app.state.log_sink
    first = sink.append(level="INFO", logger="t", message="one")
    second = sink.append(level="INFO", logger="t", message="two")

    body = client.get("/api/logs").json()
    assert [entry["message"] for entry in body["logs"]] == ["one", "two"]
    assert body["next"] == second
    assert all({"id", "at", "level", "logger", "message"} <= set(entry) for entry in body["logs"])

    page = client.get(f"/api/logs?after={body['next']}").json()
    assert page["logs"] == []
    assert page["next"] == second

    tail = client.get(f"/api/logs?after={first}").json()
    assert [entry["message"] for entry in tail["logs"]] == ["two"]


def test_api_logs_rejects_bad_cursor(client: TestClient) -> None:
    response = client.get("/api/logs?after=-2")

    assert response.status_code == 400
    assert response.json()["status"] == "error"


# --------------------------------------------------------------------------- #
# 服务生命周期落日志
# --------------------------------------------------------------------------- #


def test_chat_turn_lifecycle_is_logged(tmp_path: Path) -> None:
    plan = Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "x.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
                gated=True,
            ),
        )
    )
    app_obj = build_app(tmp_path, planner=ScriptedPlanner([plan]), router=FixedRouter())
    api = create_app(root=tmp_path, app=app_obj)
    sink = LogSink()
    detach = attach(sink)
    try:
        with TestClient(api) as test_client:
            response = test_client.post(
                "/chat",
                json={"text": "跑回归测试", "answers": {"repo_path": ".", "target": "tests"}},
            )
            assert response.status_code == 200
    finally:
        detach()
        app_obj.close()

    messages = [entry.message for entry in sink.since(-1)[0]]
    assert any("chat turn start" in message for message in messages)
    assert any("chat turn done" in message for message in messages)


def test_unhandled_exception_is_logged_with_stack(tmp_path: Path) -> None:
    class BoomSession:
        _pending = None
        session_id = "s_boom"

        def handle_turn(self, text, answers=None):
            raise RuntimeError("boom")

    class BoomApp:
        session = BoomSession()
        world = None
        root = tmp_path

        def stack_for(self, session_id=None):
            class _Stack:
                session = BoomSession()

            return _Stack()

    api = create_app(root=tmp_path, app=BoomApp())
    sink = LogSink()
    detach = attach(sink)
    try:
        with TestClient(api, raise_server_exceptions=False) as test_client:
            response = test_client.post("/chat", json={"text": "你好"})
    finally:
        detach()

    assert response.status_code == 500
    errors = [entry for entry in sink.since(-1)[0] if entry.level == "ERROR"]
    assert errors
    assert "RuntimeError" in errors[-1].message
    assert "boom" in errors[-1].message


# --------------------------------------------------------------------------- #
# 敏感值守卫：审批 token 永不进日志
# --------------------------------------------------------------------------- #


def test_approval_token_never_reaches_logs(tmp_path: Path) -> None:
    app_obj = build_app(tmp_path)
    api = create_app(root=tmp_path, app=app_obj)
    try:
        with TestClient(api) as test_client:
            plan = Plan(
                steps=(
                    Step(
                        id="write",
                        tool="fs.write",
                        args={"path": "x.txt", "content": "hi"},
                        verify=Verify("ok", needs=("ok",)),
                        gated=True,
                    ),
                )
            )
            started = app_obj.runner.start_and_run(plan)
            assert started.status == "waiting_human"
            token = app_obj.world.runs[started.run_id].approval["token"]

            body = test_client.get("/api/logs").json()
    finally:
        app_obj.close()

    assert token not in repr(body)


def test_decision_and_cancel_are_logged(tmp_path: Path) -> None:
    plan = Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "x.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
                gated=True,
            ),
        )
    )
    app_obj = build_app(tmp_path, planner=ScriptedPlanner([plan]), router=FixedRouter())
    api = create_app(root=tmp_path, app=app_obj)
    sink = LogSink()
    detach = attach(sink)
    run_id = ""
    try:
        with TestClient(api) as test_client:
            first = test_client.post(
                "/chat",
                json={"text": "跑回归测试", "answers": {"repo_path": ".", "target": "tests"}},
            )
            assert first.status_code == 200
            run_id = first.json()["run_id"]
            assert run_id
            response = test_client.post(
                f"/runs/{run_id}/cancel", json={"reason": "测试取消"}
            )
            assert response.status_code == 200
    finally:
        detach()
        app_obj.close()

    messages = [entry.message for entry in sink.since(-1)[0]]
    assert any("cancel" in message and run_id in message for message in messages)
