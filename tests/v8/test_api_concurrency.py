"""面板读投影 vs Writer 写投影的并发（D-045）。

现场（2026-11-15 18:59，qwen 在线，面板每 3s 轮询 `/api/runs`）：

    File "jagent/api/app.py", line 276, in list_runs
      for run in app_obj.world.runs.values():
    RuntimeError: dictionary changed size during iteration

FastAPI 的同步端点跑在线程池里，Writer 在另一个线程往同一个 `World.runs` 里插新 run。
这是**读中间态**：不是终态被改写（I1 没破），而是投影的读写在两个线程上没有互斥。
修法：`World.lock`（RLock）+ `World.snapshot_runs()`，写侧在锁内 apply，读侧在锁内拷贝。
"""

from __future__ import annotations

import threading

from starlette.testclient import TestClient

from jagent.api import create_app
from jagent.contracts import EventType
from jagent.wiring import build_app


def test_api_runs_survives_concurrent_run_creation(tmp_path) -> None:
    app_obj = build_app(tmp_path)
    api = create_app(root=tmp_path, app=app_obj)
    failures: list[BaseException] = []

    def create_runs() -> None:
        try:
            for index in range(60):
                run_id = f"run_{index:04d}"
                app_obj.writer.emit(
                    EventType.RUN_STARTED,
                    {"run_id": run_id, "kind": "execute", "env": {}},
                    run_id=run_id,
                )
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程断言
            failures.append(exc)

    worker = threading.Thread(target=create_runs)
    worker.start()
    try:
        with TestClient(api) as client:
            for _ in range(60):
                response = client.get("/api/runs")
                assert response.status_code == 200
    finally:
        worker.join(timeout=30)
        app_obj.close()

    assert not failures, f"写线程失败：{failures!r}"


def test_world_snapshot_is_reentrant(tmp_path) -> None:
    """RLock：读侧在快照里再取快照不能自锁（避免以后嵌套调用踩坑）。"""

    app_obj = build_app(tmp_path)
    try:
        app_obj.writer.emit(
            EventType.RUN_STARTED,
            {"run_id": "run_x", "kind": "execute", "env": {}},
            run_id="run_x",
        )
        world = app_obj.world
        with world.lock:
            assert [run.run_id for run in world.snapshot_runs()] == ["run_x"]
    finally:
        app_obj.close()
