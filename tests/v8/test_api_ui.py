"""JAgent v8 · 面板只读接口（/api/runs 与 /api/runs/{id}/events）。

这些接口只服务控制面板：列表按“最近动过”排序，事件流按 seq 增量读取。
测试只走 HTTP，和真实前端看到的是同一份结构。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jagent.api import create_app
from jagent.contracts import Plan, Step, Verify
from jagent.wiring import build_app


@pytest.fixture()
def client(tmp_path: Path):
    app_obj = build_app(tmp_path)
    api = create_app(root=tmp_path, app=app_obj)
    with TestClient(api) as test_client:
        yield test_client, app_obj
    app_obj.close()


def _gated_run(app_obj, step_id: str = "write"):
    """造一条停在人工闸口的 run；审批事件里带 token，正适合验证脱敏。"""
    plan = Plan(
        steps=(
            Step(
                id=step_id,
                tool="fs.write",
                args={"path": f"{step_id}.txt", "content": "hi"},
                verify=Verify("ok", needs=("ok",)),
                gated=True,
            ),
        )
    )
    outcome = app_obj.runner.start_and_run(plan)
    assert outcome.status == "waiting_human"
    return outcome.run_id


# --------------------------------------------------------------------------- #
# /api/runs
# --------------------------------------------------------------------------- #


def test_list_runs_shape_and_order(client) -> None:
    test_client, app_obj = client
    first = _gated_run(app_obj, "first")
    second = _gated_run(app_obj, "second")

    response = test_client.get("/api/runs")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"runs"}

    rows = body["runs"]
    assert [row["run_id"] for row in rows] == [second, first]
    assert set(rows[0]) == {"run_id", "status", "summary", "updated_seq"}
    assert rows[0]["status"] == "waiting_human"
    assert rows[0]["summary"] == ""
    assert isinstance(rows[0]["updated_seq"], int)
    assert rows[0]["updated_seq"] > rows[1]["updated_seq"]


def test_list_runs_honours_limit(client) -> None:
    test_client, app_obj = client
    first = _gated_run(app_obj, "first")
    second = _gated_run(app_obj, "second")

    rows = test_client.get("/api/runs", params={"limit": 1}).json()["runs"]

    assert len(rows) == 1
    assert rows[0]["run_id"] == second
    assert rows[0]["run_id"] != first


# --------------------------------------------------------------------------- #
# /api/runs/{id}/events
# --------------------------------------------------------------------------- #


def test_events_are_incremental_and_never_leak_the_token(client) -> None:
    test_client, app_obj = client
    run_id = _gated_run(app_obj)
    token = app_obj.world.runs[run_id].approval["token"]

    first_response = test_client.get(f"/api/runs/{run_id}/events", params={"after_seq": 0})
    assert first_response.status_code == 200
    first = first_response.json()
    assert set(first) == {"events", "next_seq"}

    events = first["events"]
    assert events, "一次审批挂起至少应产生 run.started / plan.frozen / approval.requested"
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    assert first["next_seq"] == seqs[-1]
    assert "approval.requested" in {event["type"] for event in events}

    for event in events:
        assert set(event) == {"seq", "type", "at", "payload"}
        assert isinstance(event["payload"], dict)
        assert "token" not in event["payload"]

    assert token not in first_response.text, "事件流不能把审批 token 发给浏览器"

    # Decide, then read only what came after the first batch.
    decided = test_client.post(
        f"/runs/{run_id}/decision",
        json={"decision": "approve", "comment": ""},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "answered"

    second_response = test_client.get(
        f"/api/runs/{run_id}/events",
        params={"after_seq": first["next_seq"]},
    )
    assert second_response.status_code == 200
    second = second_response.json()
    second_seqs = [event["seq"] for event in second["events"]]

    assert second_seqs, "批准之后应该出现 approval.received / step.verified / run.completed"
    assert min(second_seqs) > max(seqs)
    assert set(seqs).isdisjoint(second_seqs)
    assert second_seqs == sorted(second_seqs)
    assert second["next_seq"] == second_seqs[-1]
    assert token not in second_response.text

    # Polling once more with the newest cursor is empty and idempotent.
    third = test_client.get(
        f"/api/runs/{run_id}/events",
        params={"after_seq": second["next_seq"]},
    ).json()
    assert third["events"] == []
    assert third["next_seq"] == second["next_seq"]


def test_events_for_unknown_run_is_404(client) -> None:
    test_client, _app_obj = client

    response = test_client.get("/api/runs/run_does_not_exist/events")

    assert response.status_code == 404
    assert response.json()["status"] == "error"
