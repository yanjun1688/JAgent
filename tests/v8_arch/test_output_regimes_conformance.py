"""输出分级的机械门禁（宪法 v2 I9；输出分级 v1 §8）。

三条规则，全部可机械检查，不靠自觉：

1. `advice.delivered` 的必填字段里有 `delivery`——少一条下游就无法判断可信度；
2. Writer 会拒绝缺 `delivery` 的投递事件（契约由 `contracts.REQUIRED_FIELDS` 单一真源执行）；
3. `gate/policy.md` 的分级登记表覆盖每个必需 prompt，且每条约束映射到 check id 或显式
   登记为"无 sensor"——**只有约束没有检查 = 没控制**（I9）。

外加两条防回归：证据构造不得读投递/报告文本（污点规则）；前端必须消费 `regime`
（否则"未核验"只写在 API 里，用户看不见）。
"""

from __future__ import annotations

import pathlib

import pytest

from jagent.config import REQUIRED_PROMPTS
from jagent.contracts import REQUIRED_FIELDS, EventType
from jagent.grounding import CHECK_CITATIONS, CHECK_DIRECTIVES, CHECK_NUMBERS
from jagent.harness.fold import World
from jagent.harness.store import EventStore
from jagent.harness.writer import Writer

REPO = pathlib.Path(__file__).resolve().parents[2]
POLICY = REPO / "jagent" / "gate" / "policy.md"
SESSION = REPO / "jagent" / "app" / "session.py"
KNOWN_CHECKS = {CHECK_NUMBERS, CHECK_CITATIONS, CHECK_DIRECTIVES}


def test_advice_delivered_requires_a_delivery_field() -> None:
    assert "delivery" in REQUIRED_FIELDS[EventType.ADVICE_DELIVERED], (
        "advice.delivered 缺 delivery 必填字段：regime 会变成可省略的装饰"
    )


def test_writer_rejects_a_delivery_without_regime(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    writer = Writer(store, World(), "s_conf")
    try:
        with pytest.raises(Exception):
            writer.emit(
                EventType.ADVICE_DELIVERED,
                {"advice": {"body": "x"}, "mode": "report"},  # 没有 delivery
            )
    finally:
        store.close()


def _registry_rows() -> list[list[str]]:
    text = POLICY.read_text(encoding="utf-8")
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def test_policy_registers_every_prompt_with_a_sensor_or_explicit_unverified() -> None:
    rows = _registry_rows()
    registered = {cells[0].strip("`") for cells in rows if cells}
    missing = [name for name in REQUIRED_PROMPTS if name not in registered]
    assert not missing, (
        f"policy.md 的分级登记表没覆盖这些 prompt：{missing}（I9：约束必须有 sensor，"
        "或显式登记为无 sensor）"
    )
    for cells in rows:
        if cells[0].strip("`") not in REQUIRED_PROMPTS or len(cells) < 3:
            continue
        sensor = cells[-1]
        assert sensor.split()[0] in KNOWN_CHECKS or sensor.startswith("unverified"), (
            f"{cells[0]} 的 sensor 列既不是已知 check id 也不是显式 unverified：{sensor!r}"
        )


def test_evidence_builders_never_read_delivery_or_report_text() -> None:
    """污点规则：证据只从 step.finished 取，不读投递/报告（措辞不是事实）。"""

    source = SESSION.read_text(encoding="utf-8")
    for name in ("_evidence_of", "_evidence_bundle", "_evidence_item"):
        start = source.find(f"def {name}(")
        assert start >= 0, f"找不到 {name}"
        tail = source[start:]
        end = tail.find("\n    def ", 10)
        body = tail if end < 0 else tail[:end]
        assert "ADVICE_DELIVERED" not in body, f"{name} 读了投递文本（污点违规）"
        assert "REPORT_RENDERED" not in body, f"{name} 读了报告文本（污点违规）"


def test_ui_consumes_the_regime_field() -> None:
    """前端必须消费 regime：否则"未核验"只存在于 API，用户看不到。"""

    api_ts = (REPO / "ui" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "regime" in api_ts, "ui/src/lib/api.ts 的类型里没有 delivery.regime"
    app_tsx = (REPO / "ui" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "regime" in app_tsx, "App.tsx 没有渲染三档徽标"
