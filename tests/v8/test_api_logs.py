"""JAgent v8 · /api/logs 日志出口测试（logsink 管道）。

日志是运维观测（服务生命周期、执行链路 route→plan→step→verify→failure→
run 终态、模型调用、异常堆栈），**不是事件**：零写事件库、零落盘，进程重启
即清空，只留一个"当前控制台"窗口。

这里钉住六件事：
1. 环形缓冲的游标语义（monotonic id + since(after) + 满员丢最老）；
2. /api/logs 端点契约（{logs, next}，坏游标友好拒绝）；
3. 服务生命周期落日志（chat 轮次、未捕获异常带堆栈）；
4. 敏感值（审批 token）永远不进日志——只有失守才会出现的信号；
5. uvicorn 访问线不进窗口（面板自指噪音），uvicorn 生命周期照常；
6. 一次失败 run 的完整调用链（D-039）+ /api/logs/stream SSE 回放。
"""

from __future__ import annotations

import json
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
from tests.support import DraftPlanner, FixedRouter


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


# --------------------------------------------------------------------------- #
# 访问线排除（D-039）：面板只看系统在做什么，不看面板自己刷新自己
# --------------------------------------------------------------------------- #


def test_access_lines_stay_out_of_sink() -> None:
    sink = LogSink()
    detach = attach(sink)
    try:
        logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /api/logs 200"')
        logging.getLogger("uvicorn.error").info("Application startup complete.")
        logging.getLogger("jagent.chain").info("route intent=execute")
    finally:
        detach()

    messages = [entry.message for entry in sink.since(-1)[0]]
    assert any("Application startup complete." in m for m in messages)
    assert any("route intent=execute" in m for m in messages)
    assert not any("GET /api/logs" in m for m in messages)


# --------------------------------------------------------------------------- #
# 失败 run 的完整调用链（D-039）：看日志就知道为什么失败
# --------------------------------------------------------------------------- #


def test_failed_run_produces_call_chain_logs(tmp_path: Path) -> None:
    plan = Plan(
        steps=(
            Step(
                id="list_dir",
                tool="fs.list",
                args={"path": "."},
                verify=Verify("len(entries) > 999", needs=("entries",)),
            ),
        )
    )
    app_obj = build_app(tmp_path, planner=DraftPlanner([plan]), router=FixedRouter())
    api = create_app(root=tmp_path, app=app_obj)
    sink = LogSink()
    detach = attach(sink)
    try:
        with TestClient(api) as test_client:
            response = test_client.post("/chat", json={"text": "跑回归测试", "answers": None})
            assert response.status_code == 200
            assert response.json()["status"] == "answered"
    finally:
        detach()
        app_obj.close()

    entries, _ = sink.since(-1)
    messages = [entry.message for entry in entries]
    levels = {entry.message: entry.level for entry in entries}
    # 完整链路：路由 → 计划 → 步骤起止 → 校验失败 → 分类 → run 终态
    assert any("route intent=" in m for m in messages)
    assert any("plan frozen" in m and "list_dir" in m for m in messages)
    assert any("step started" in m and "list_dir" in m for m in messages)
    assert any("verify FAIL" in m for m in messages)
    assert any("failure classified" in m for m in messages)
    failed_line = next(m for m in messages if "run FAILED" in m)
    assert "reason=" in failed_line and "list_dir" in failed_line
    # 失败相关行的级别语义：verify/classified = WARNING，终态 = ERROR
    assert levels[failed_line] == "ERROR"
    verify_line = next(m for m in messages if "verify FAIL" in m)
    assert levels[verify_line] == "WARNING"


# --------------------------------------------------------------------------- #
# SSE 推流（D-039）：连上先回放存量，面板不再轮询
# --------------------------------------------------------------------------- #


def test_sse_stream_replays_backlog_and_pushes_live(tmp_path: Path) -> None:
    """推流逻辑（模块级 sse_stream）：先回放存量，再实时推送落地的新行。

    直接用 asyncio.run 驱动生成器，不走 TestClient 流式读取——anyio portal
    对无限/有界流式响应的清理在 Windows 上会挂死（环境层问题，机制无关）。
    """
    import asyncio

    from jagent.api.app import sse_stream

    sink = LogSink()
    first = sink.append(level="INFO", logger="t", message="backlog line")

    async def collect() -> list[str]:
        gen = sse_stream(sink, after=-1, max_events=2)
        lines: list[str] = []

        async def feed() -> None:
            await asyncio.sleep(0.05)
            sink.append(level="INFO", logger="t", message="live line")

        feeder = asyncio.ensure_future(feed())
        async for line in gen:
            lines.append(line)
        await feeder
        return lines

    lines = asyncio.run(collect())
    data_lines = [line for line in lines if line.startswith("data: ")]
    assert len(data_lines) == 2, "回放 1 条 + 实时 1 条 = 2 条"

    replayed = json.loads(data_lines[0][len("data: "):])
    assert replayed["id"] == first
    assert replayed["message"] == "backlog line"

    live = json.loads(data_lines[1][len("data: "):])
    assert live["message"] == "live line"
    assert live["id"] > replayed["id"]


def test_model_call_logs_prompt_and_reply(tmp_path: Path) -> None:
    """LLM 的请求与响应原文进日志详情块（D-040）；失败的调用带 prompt。"""
    sink = LogSink()
    detach = attach(sink)
    try:
        from jagent.harness.observability import ObservationModel
        from jagent.wiring import App  # noqa: F401 - 确认装配可用

        class FakeReply:
            text = "好的，这就去列目录。"
            tokens_in = 10
            tokens_out = 5

        class FakeModel:
            name = "fake"

            def complete(self, *, system: str, prompt: str, temperature: float = 0.0):
                return FakeReply()

        class SilentWriter:
            def emit(self, *_args, **_kwargs) -> None:
                pass

        view = ObservationModel(
            FakeModel(), purpose="router", writer=SilentWriter(), artifacts=None
        )
        view.complete(system="你是路由器", prompt="用户想看目录", temperature=0.0)
    finally:
        detach()

    entries, _ = sink.since(-1)
    joined = "\n".join(entry.message for entry in entries)
    assert "model.call" in joined
    assert "── prompt ──" in joined
    assert "你是路由器" in joined
    assert "── reply ──" in joined
    assert "这就去列目录" in joined
