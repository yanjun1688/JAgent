"""可观测性 S2 · 指标投影（D-031）。

``jagent/metrics.py`` 是**纯派生**：路线图 14 的三原语在事件日志里已齐备，
metrics 只做聚合投影，零采集。基数纪律（CNCF）：session_id/run_id 不进
指标标签——聚合只留计数，单 run 细节走事件流（Trace）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jagent.api import create_app
from jagent.contracts import Budget, IntentType, Plan, ReplyDraft, Step, Verify
from jagent.metrics import run_metrics, session_metrics
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter


def _completed_run_app(tmp_path):
    plan = Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "done.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
            ),
        ),
    )
    app = build_app(
        tmp_path,
        planner=DraftPlanner([plan, ReplyDraft(text="写好了。")]),
        router=FixedRouter(IntentType.EXECUTE),
        budget=Budget(auto_approve_tools=("fs.write",)),
    )
    out = app.session.handle_turn("写 done.txt")
    assert out.status == "answered"
    return app


def test_session_metrics_counts_the_four_faces(tmp_path) -> None:
    """Given 一轮「建 run + 审批 + 完成」的对话，When session_metrics，
    Then 成本/效率/质量/异常四面计数与事件一致（纯派生，零采集）。"""
    app = _completed_run_app(tmp_path)
    try:
        metrics = session_metrics(app.store.read())
        assert metrics["turns"] == 1
        assert metrics["runs"] == 1
        assert metrics["verdicts"]["completed"] == 1
        assert metrics["completion_rate"] == 1.0
        assert metrics["human_interventions"] == 0
        assert metrics["repairs"] == 0.0
        assert metrics["model_calls"] == 0, "无模型装配没有 model.called"
    finally:
        app.close()


def test_run_metrics_reports_verdict_steps_budget_and_wall_time(tmp_path) -> None:
    """Given 一个完成的 run，When run_metrics，
    Then verdict/steps(passed 计数)/budget(steps/wall_s)/时长齐全。"""
    app = _completed_run_app(tmp_path)
    try:
        run_id = next(iter(app.world.runs))
        metrics = run_metrics(app.store.read(), run_id)
        assert metrics["verdict"] == "completed"
        assert metrics["steps"]["passed"] == 1
        assert metrics["budget"]["steps"] == 1.0
        assert metrics["budget"]["wall_s"] > 0
        assert metrics["wall_s"] > 0
        assert metrics["invalidations"] == 0
        assert run_metrics(app.store.read(), "run_missing") == {}
    finally:
        app.close()


def test_metrics_api_endpoints(tmp_path) -> None:
    """Given HTTP 面，When 打两个 metrics 端点，Then 会话级与 run 级投影可达，
    未知 run 404（与其他端点同一错误口径）。"""
    api = create_app(root=tmp_path)
    with TestClient(api) as client:
        app_obj = client.app.state.jagent
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
        (tmp_path / "ws" / "note.md").write_text("x\n", encoding="utf-8")
        outcome = app_obj.runner.start_and_run(plan)
        assert outcome.status == "completed"

        session = client.get("/api/metrics/session")
        assert session.status_code == 200
        body = session.json()
        assert body["runs"] >= 1 and body["verdicts"]["completed"] >= 1

        run = client.get(f"/api/runs/{outcome.run_id}/metrics")
        assert run.status_code == 200
        assert run.json()["verdict"] == "completed"

        missing = client.get("/api/runs/run_missing/metrics")
        assert missing.status_code == 404
        app_obj.close()


def test_session_metrics_counts_delivery_regimes(tmp_path) -> None:
    """Given 一条 grounded 复述与一条 unverified 闲聊，When session_metrics，
    Then 三档计数可见（有界枚举，符合 D-031 基数纪律）——"多少输出没核验"可度量。"""

    from jagent.contracts import Delivery, EventType, Regime
    from jagent.harness.fold import World
    from jagent.harness.store import EventStore
    from jagent.harness.writer import Writer

    store = EventStore(tmp_path / "events.db")
    writer = Writer(store, World(), "s_regime")
    try:
        for delivery in (
            Delivery(text="读到了 17.19。", regime=Regime.GROUNDED),
            Delivery(text="我觉得可以再想想。", regime=Regime.UNVERIFIED),
            Delivery(text="1/1 步通过", regime=Regime.VERIFIED),
        ):
            writer.emit(
                EventType.ADVICE_DELIVERED,
                {
                    "advice": {"body": delivery.text},
                    "mode": "report",
                    "delivery": {
                        "regime": delivery.regime.value,
                        "checks": [],
                        "grounds": [],
                        "fallback": {},
                    },
                },
            )
        metrics = session_metrics(store.read())
    finally:
        store.close()

    assert metrics["delivery_regimes"] == {"verified": 1, "grounded": 1, "unverified": 1}
