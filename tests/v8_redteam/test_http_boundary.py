"""I13 · HTTP 边界：输入校验、404/409、错误响应不泄露内部信息。

规格依据：
- §4.7 Executor / 人工确认中心：相同确认重复到达不重复执行（HTTP 面应返回 409，
  而不是二次执行）；
- api/app.py 自述「不暴露事件、token、错误堆栈或内部术语」；
- 任务 3：空 body、非 JSON、100KB 文本、未知 slot、不存在的 run、重复决策。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jagent.api import create_app
from jagent.contracts import EventType, Plan, Step, Verify
from jagent.wiring import build_app

_LEAK_MARKERS = (
    "Traceback",
    "site-packages",
    "File \"",
    "jagent/",
    "jagent\\",
    ".py\", line",
)


def _assert_no_internal_leak(text: str, tmp_path) -> None:
    for marker in _LEAK_MARKERS:
        assert marker not in text, f"错误响应泄露内部信息：{marker!r}"
    assert str(tmp_path) not in text, "错误响应泄露了服务器路径"


@pytest.fixture()
def api(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    app = build_app(root)
    api = create_app(root=root, app=app)
    with TestClient(api) as client:
        yield client, app
    app.close()


def _gated_run(app):
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
    return app.runner.start_and_run(plan)


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #

def test_empty_json_body_returns_friendly_400(api, tmp_path) -> None:
    client, _app = api

    response = client.post("/chat", json={})

    assert response.status_code == 400
    assert response.json()["status"] == "error"
    assert response.json()["message"]
    _assert_no_internal_leak(response.text, tmp_path)


def test_non_json_body_is_rejected_without_a_stack_trace(api, tmp_path) -> None:
    client, _app = api

    response = client.post("/chat", content=b"this is not json", headers={"content-type": "text/plain"})

    assert response.status_code in (400, 422)
    _assert_no_internal_leak(response.text, tmp_path)


def test_100kb_text_is_handled_without_a_500(api, tmp_path) -> None:
    client, _app = api
    text = "跑回归测试" * 20_000  # > 100KB UTF-8

    response = client.post("/chat", json={"text": text})

    assert response.status_code in (200, 400)
    assert response.status_code != 500
    assert len(response.text) < 10_000, "响应不应把 100KB 输入原样回显"
    _assert_no_internal_leak(response.text, tmp_path)


def test_oversized_input_is_not_stored_verbatim(api) -> None:
    client, app = api
    text = "跑回归测试" * 20_000

    client.post("/chat", json={"text": text})

    received = [event for event in app.store.read() if event.type is EventType.TURN_RECEIVED]
    assert received
    assert len(received[-1].payload.get("text", "")) <= 8_000, "输入原文不应无上限进事件"


def test_unknown_slot_in_answers_does_not_crash(api, tmp_path) -> None:
    client, _app = api

    response = client.post("/chat", json={"text": "你好", "answers": {"__unknown_slot__": "x"}})

    assert response.status_code == 200
    assert response.json()["status"] in ("needs_input", "answered")
    _assert_no_internal_leak(response.text, tmp_path)


def test_unknown_slot_in_answers_is_ignored(api) -> None:
    client, app = api

    client.post("/chat", json={"text": "你好", "answers": {"__unknown_slot__": "x"}})

    assert "__unknown_slot__" not in app.session.state.slots


def test_waiting_run_shows_the_command_to_approve(api) -> None:
    client, app = api
    plan = Plan(
        steps=(
            Step(
                id="danger",
                tool="shell.exec",
                args={"cmd": "echo UNIQUE-MARKER-COMMAND"},
                verify=Verify("exit_code == 0", needs=("exit_code",)),
                gated=True,
            ),
        )
    )
    started = app.runner.start_and_run(plan)
    assert started.status == "waiting_human"

    response = client.get(f"/runs/{started.run_id}")

    assert response.status_code == 200
    assert "UNIQUE-MARKER-COMMAND" in response.text, "同意之前必须看得到要执行的命令"


# --------------------------------------------------------------------------- #
# 404 / 409 / 重复决策
# --------------------------------------------------------------------------- #

def test_unknown_run_is_404(api, tmp_path) -> None:
    client, _app = api

    response = client.get("/runs/run_does_not_exist")

    assert response.status_code == 404
    _assert_no_internal_leak(response.text, tmp_path)


def test_unknown_run_decision_is_404(api, tmp_path) -> None:
    client, _app = api

    response = client.post(
        "/runs/run_does_not_exist/decision", json={"decision": "approve", "comment": ""}
    )

    assert response.status_code == 404
    _assert_no_internal_leak(response.text, tmp_path)


def test_duplicate_decision_is_409(api) -> None:
    client, app = api
    started = _gated_run(app)
    assert started.status == "waiting_human"

    first = client.post(f"/runs/{started.run_id}/decision", json={"decision": "approve"})
    second = client.post(f"/runs/{started.run_id}/decision", json={"decision": "approve"})

    assert first.status_code == 200
    assert second.status_code == 409, "重复决策不能二次执行"
    # 只允许一次生效的 approval.received。
    applied = [
        event
        for event in app.store.read()
        if event.type is EventType.APPROVAL_RECEIVED and event.run_id == started.run_id
    ]
    assert len(applied) == 1


def test_decision_on_a_run_without_pending_approval_is_409(api) -> None:
    client, app = api
    plan = Plan(
        steps=(
            Step(
                id="list",
                tool="fs.list",
                args={"path": "."},
                verify=Verify("len(entries) >= 0", needs=("entries",)),
            ),
        )
    )
    outcome = app.runner.start_and_run(plan)
    assert outcome.status == "completed"

    response = client.post(f"/runs/{outcome.run_id}/decision", json={"decision": "approve"})

    assert response.status_code == 409


def test_unknown_decision_word_is_400(api, tmp_path) -> None:
    client, app = api
    started = _gated_run(app)

    response = client.post(f"/runs/{started.run_id}/decision", json={"decision": "maybe"})

    assert response.status_code == 400
    _assert_no_internal_leak(response.text, tmp_path)


def test_run_view_does_not_leak_the_approval_token(api) -> None:
    client, app = api
    started = _gated_run(app)
    token = app.world.runs[started.run_id].approval["token"]

    response = client.get(f"/runs/{started.run_id}")

    assert response.status_code == 200
    assert token not in response.text
    assert response.json()["needs_human"] is True


def test_validation_errors_do_not_echo_the_body(api, tmp_path) -> None:
    client, _app = api
    secret = "SUPER-SECRET-MARKER"

    response = client.post(
        "/chat", content=secret.encode(), headers={"content-type": "application/json"}
    )

    assert response.status_code in (400, 422)
    _assert_no_internal_leak(response.text, tmp_path)
    assert secret not in response.text
