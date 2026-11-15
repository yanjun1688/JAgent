"""JAgent v8 · HTTP 产品面测试。

只走 HTTP：不依赖真实模型，也不直接读事件日志。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jagent.agent.planner import ScriptedPlanner
from jagent.api import create_app
from jagent.contracts import Budget, ClarifyDraft, Plan, Question, Step, Verify
from jagent.wiring import build_app
from tests.support import DraftPlanner, FixedRouter


@pytest.fixture()
def client(tmp_path: Path):
    # build_app 的 workspace 是 <root>/ws；回归 recipe 会在这里跑 pytest。
    ws_tests = tmp_path / "ws" / "tests"
    ws_tests.mkdir(parents=True, exist_ok=True)
    (ws_tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    api = create_app(root=tmp_path)
    with TestClient(api) as test_client:
        yield test_client
    api.state.jagent.close()


def _start_gated_run(client: TestClient):
    app_obj = client.app.state.jagent
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
    return app_obj.runner.start_and_run(plan), app_obj


# --------------------------------------------------------------------------- #
# 健康检查与静态页
# --------------------------------------------------------------------------- #


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "ui" / "dist" / "index.html").is_file(),
    reason="ui/dist 是本地构建产物（不入库）：公开树只有内置单文件页，无 assets 引用",
)
def test_root_serves_offline_html(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    # ui/dist is built locally: a module script / stylesheet under /assets is
    # fine, an external http(s) resource (or a CDN host) is not.
    assert "assets/" in html
    assert 'type="module"' in html
    assert "http://" not in html
    assert "https://" not in html
    assert "cdn." not in html.lower()


def test_root_falls_back_to_bundled_page_without_ui_dist(tmp_path: Path, monkeypatch) -> None:
    """No build output => the old single-file page still answers `/`."""
    import jagent.api.app as api_module

    monkeypatch.setattr(api_module, "UI_DIST", tmp_path / "no-ui-dist")
    root = tmp_path / "root"
    app_obj = build_app(root)
    api = create_app(root=root, app=app_obj)
    try:
        with TestClient(api) as test_client:
            response = test_client.get("/")
    finally:
        app_obj.close()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "JAgent 助手" in response.text
    assert "assets/" not in response.text


def test_cors_allows_any_origin(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Origin": "http://example.com"})

    assert response.headers.get("access-control-allow-origin") == "*"


# --------------------------------------------------------------------------- #
# /chat：两条主路径 + 输入校验
# --------------------------------------------------------------------------- #


def test_chat_regression_asks_then_runs(tmp_path: Path) -> None:
    # 产品默认要求不可逆命令人工确认；测试里显式放行 shell.exec，
    # 只验证 /chat 的完整契约（needs_input → answers → answered + result）。
    # 澄清问题由 planner 的 clarify 草稿给出（静态槽位表已随模板层删除）。
    ws_tests = tmp_path / "ws" / "tests"
    ws_tests.mkdir(parents=True, exist_ok=True)
    (ws_tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    plan = Plan(steps=(
        Step(
            id="find_tests",
            tool="fs.list",
            args={"path": "tests"},
            verify=Verify("len(entries) > 0", needs=("entries",)),
        ),
        Step(
            id="run_tests",
            tool="shell.exec",
            args={"cmd": "python -m pytest tests -q"},
            depends_on=("find_tests",),
            verify=Verify("exit_code == 0", needs=("exit_code",)),
        ),
    ))
    clarify = ClarifyDraft(
        questions=(Question(slot="target", text="跑哪个目录的测试？", options=("tests",)),)
    )
    app_obj = build_app(
        tmp_path,
        planner=DraftPlanner([clarify, plan]),
        router=FixedRouter(),
        budget=Budget(auto_approve_tools=("shell.exec",)),
    )
    api = create_app(root=tmp_path, app=app_obj)
    try:
        with TestClient(api) as test_client:
            first = test_client.post("/chat", json={"text": "跑回归测试", "answers": None})
            assert first.status_code == 200
            asked = first.json()
            assert asked["status"] == "needs_input"
            assert asked["slot"]
            assert asked["choices"]
            assert asked["message"]
            assert all({"label", "value"} <= set(choice) for choice in asked["choices"])

            second = test_client.post(
                "/chat",
                json={"text": "跑回归测试", "answers": {"target": "tests"}},
            )
            assert second.status_code == 200
            answered = second.json()
            assert answered["status"] == "answered"
            assert answered["run_id"].startswith("run_")
            assert answered["message"] == answered["result"]["summary"]
            assert answered["result"]["verdict"] == "completed"
            assert answered["result"]["steps"] == [
                {"id": "find_tests", "status": "passed"},
                {"id": "run_tests", "status": "passed"},
            ]
    finally:
        app_obj.close()


def test_chat_stock_question_asks_with_choices(tmp_path: Path) -> None:
    clarify = ClarifyDraft(
        questions=(
            Question(
                slot="ticker",
                text="你想分析哪只标的？",
                options=("沪深300ETF", "标普500ETF", "某只个股（回复代码）"),
            ),
        )
    )
    app_obj = build_app(tmp_path, planner=DraftPlanner([clarify]), router=FixedRouter())
    api = create_app(root=tmp_path, app=app_obj)
    try:
        with TestClient(api) as test_client:
            response = test_client.post("/chat", json={"text": "这支股票值得买吗", "answers": None})
    finally:
        app_obj.close()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_input"
    assert body["slot"] == "ticker"
    assert body["choices"]
    assert all(choice["label"] == choice["value"] for choice in body["choices"])


def test_chat_empty_text_is_friendly_400(client: TestClient) -> None:
    response = client.post("/chat", json={"text": "   ", "answers": None})

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["message"]


def test_chat_missing_text_is_friendly_400(client: TestClient) -> None:
    response = client.post("/chat", json={})

    assert response.status_code == 400
    assert response.json()["status"] == "error"


# --------------------------------------------------------------------------- #
# /runs：查询与人工决策
# --------------------------------------------------------------------------- #


def test_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/runs/run_does_not_exist")

    assert response.status_code == 404
    assert response.json()["status"] == "error"


def test_gated_run_can_be_approved_over_http(client: TestClient) -> None:
    started, app_obj = _start_gated_run(client)
    assert started.status == "waiting_human"

    before = client.get(f"/runs/{started.run_id}")
    assert before.status_code == 200
    before_body = before.json()
    assert before_body["status"] == "waiting_human"
    assert before_body["needs_human"] is True
    assert before_body["question"] is None
    token = app_obj.world.runs[started.run_id].approval["token"]
    assert token not in before.text

    response = client.post(
        f"/runs/{started.run_id}/decision",
        json={"decision": "approve", "comment": ""},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] != "waiting_human"
    assert body["status"] == "answered"
    assert body["result"]["verdict"] == "completed"
    assert body["run_id"] == started.run_id

    after = client.get(f"/runs/{started.run_id}")
    after_body = after.json()
    assert after_body["status"] == "completed"
    assert after_body["needs_human"] is False
    assert after_body["steps"] == [{"id": "write", "status": "passed"}]
    assert after_body["summary"]


def test_repeated_decision_is_409(client: TestClient) -> None:
    started, _ = _start_gated_run(client)

    first = client.post(
        f"/runs/{started.run_id}/decision",
        json={"decision": "approve", "comment": ""},
    )
    assert first.status_code == 200

    again = client.post(
        f"/runs/{started.run_id}/decision",
        json={"decision": "approve", "comment": ""},
    )
    assert again.status_code == 409
    assert again.json()["status"] == "error"


def test_unknown_decision_is_friendly_400(client: TestClient) -> None:
    started, _ = _start_gated_run(client)

    response = client.post(
        f"/runs/{started.run_id}/decision",
        json={"decision": "maybe", "comment": ""},
    )

    assert response.status_code == 400
    assert response.json()["status"] == "error"


def test_chat_waiting_human_uses_plain_message(tmp_path: Path) -> None:
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
    try:
        with TestClient(api) as test_client:
            response = test_client.post(
                "/chat",
                json={"text": "跑回归测试", "answers": {"repo_path": ".", "target": "tests"}},
            )
    finally:
        app_obj.close()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "waiting_human"
    assert body["message"] == "这一步需要你确认，确认后我继续。"
    assert body["run_id"]
    assert "choices" not in body
    assert "result" not in body


# --------------------------------------------------------------------------- #
# 未捕获异常：只回一句人话
# --------------------------------------------------------------------------- #


def test_unhandled_error_returns_friendly_500(tmp_path: Path) -> None:
    class BoomSession:
        _pending = None

        def handle_turn(self, text, answers=None):
            raise RuntimeError("boom")

    class BoomApp:
        session = BoomSession()
        world = None
        root = tmp_path

    api = create_app(root=tmp_path, app=BoomApp())
    with TestClient(api, raise_server_exceptions=False) as test_client:
        response = test_client.post("/chat", json={"text": "你好"})

    assert response.status_code == 500
    assert response.json() == {"status": "error", "message": "服务出错了，请重试"}
