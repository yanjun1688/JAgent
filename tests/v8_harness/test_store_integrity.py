"""P2-b/c · Event Log 严格 JSON 与全路径哈希校验（BUG2-07 / BUG2-10）。

BUG2-10：``append`` 用默认 ``json.dumps``（allow_nan=True），NaN/inf 落库成
非标准 JSON 字面量，``/api/runs/{id}/events`` 的 Starlette JSONResponse
（allow_nan=False）直接 500——同一条流"写得进、投不出"。修复：写库前按
严格 JSON 预序列化，非法载荷在事务外拒绝（§3.1 事件 payload 是可重放数据，
``canonical_json`` 的 allow_nan=False 是既定意图）；executor 的观测打包把
非有限浮点收敛成 null（JSON 没有 NaN，null 是"没有值"的标准表达）。

BUG2-07：哈希链从第一条锚定，``read(run_id=...)`` 的过滤读会"跳过"别的
run 的行——被篡改的行若不在过滤结果里，链断了也看不出来。修复：任何读
都先取全量、校验整条链，再在内存里过滤；篡改无论从哪个入口读都暴露。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jagent.contracts import EventType, Plan, Step, Verify, plan_to_dict
from jagent.harness.executor import _jsonable

from .conftest import RUN_ID, STEP_ID, Harness

OTHER_RUN = "run_other"


@pytest.fixture()
def two_runs(harness: Harness) -> None:
    """Given 两个 run 各有一条事件（跨 run 的链）。"""
    plan = Plan(steps=(Step(id=STEP_ID, tool="probe", verify=Verify("True", needs=())),))
    for run_id in (RUN_ID, OTHER_RUN):
        harness.writer.emit(
            EventType.RUN_STARTED,
            {"run_id": run_id, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
            run_id=run_id,
        )
        harness.writer.emit(
            EventType.PLAN_FROZEN,
            {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
            run_id=run_id,
        )


# --------------------------------------------------------------------------- #
# BUG2-10：非法载荷在日志边界被拒
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_payload_is_rejected_at_the_log_boundary(harness: Harness, bad: float) -> None:
    """Given 载荷含 NaN/inf，When emit，Then ValueError 且零副作用——
    事件日志里永远不出现非标准 JSON 字面量。"""
    before = len(harness.events())
    with pytest.raises(ValueError):
        harness.writer.emit(
            EventType.REPORT_RENDERED,
            {"result": {"value": bad}},
            run_id=RUN_ID,
        )
    assert len(harness.events()) == before, "被拒的写不能留下半条事件"


def test_executor_maps_non_finite_observations_to_null() -> None:
    """Given 工具观测里出现 NaN/inf，When _jsonable 打包，
    Then 非有限浮点收敛成 null——合法工具输出不再炸步骤，
    而写进日志的观测可被严格 JSON 解析。"""
    packed = _jsonable({"v": float("nan"), "w": float("inf"), "ok": 1.5, "nested": [float("nan")]})
    assert packed["v"] is None and packed["w"] is None and packed["ok"] == 1.5
    assert packed["nested"] == [None]
    # 收敛结果可被严格 JSON 往返
    json.dumps(packed, allow_nan=False)


# --------------------------------------------------------------------------- #
# BUG2-07：过滤读也校验整条链
# --------------------------------------------------------------------------- #

def _tamper_one_payload(db_path, seq: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT payload FROM events WHERE seq = ?", (seq,)).fetchone()
        payload = json.loads(row[0])
        payload["tampered"] = True
        conn.execute(
            "UPDATE events SET payload = ? WHERE seq = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), seq),
        )
        conn.commit()
    finally:
        conn.close()


def test_tampering_is_detected_on_every_read_path(harness: Harness, two_runs) -> None:
    """Given 篡改 OTHER_RUN 的一条 payload（digest 不再匹配），When 分别用
    run 过滤读 / 全量读 / since_seq 读，Then 全部抛错——没有绕过链的入口。"""
    tampered_seq = harness.of_type(EventType.PLAN_FROZEN)[1].seq  # OTHER_RUN 的 frozen
    _tamper_one_payload(harness.db_path, tampered_seq)

    with pytest.raises(Exception):
        harness.store.read(run_id=OTHER_RUN)
    # 篡改在别的 run 里：这一条 run 的过滤读也必须验链，不能绕过。
    with pytest.raises(Exception):
        harness.store.read(run_id=RUN_ID)
    with pytest.raises(Exception):
        harness.store.read()
    with pytest.raises(Exception):
        harness.store.read(since_seq=tampered_seq - 1)


def test_filtered_read_still_filters_after_verification(harness: Harness, two_runs) -> None:
    """Given 干净的链，When run 过滤读，Then 只返回该 run 的事件
    （校验不改变过滤语义）。"""
    events = harness.store.read(run_id=RUN_ID)
    assert events and all(e.run_id == RUN_ID for e in events)
    assert {e.type for e in events} == {EventType.RUN_STARTED, EventType.PLAN_FROZEN}
