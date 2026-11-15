"""输出分级在评估面的两个确定性 check（输出分级 v1 §7）。

评估不进事件流、不调模型：这两个 kind 只读投递事件里**代码写下的** regime 与 checks。

- ``delivery_regime``：最后一次（或任一）投递的档位是否等于期望；
- ``numbers_grounded``：凡带数字声明的投递，必须声明自己过了 `numbers.grounded`
  ——"说了数字却没做数字对账"是契约违规，可度量。
"""

from __future__ import annotations

from jagent.evaluation import score_attempt, tasks_from_mapping


def task_of(*checks):
    data = {
        "schema": "eval.tasks.v1",
        "task": {"t": {"description": "d", "turns": ["hi"], "checks": checks}},
    }
    return tasks_from_mapping(data, tasks_hash="h").tasks[0]


def run_check(rig, check: dict):
    return score_attempt(rig.events(), task_of(check)).checks[0]


def test_delivery_regime_passes_when_last_delivery_matches(rig) -> None:
    rig.advice_delivery(mode="report", passed=True, citations=1)

    result = run_check(rig, {"kind": "delivery_regime", "expect": "grounded"})

    assert result.passed
    assert result.observed == ["grounded"]


def test_delivery_regime_fails_on_wrong_regime(rig) -> None:
    rig.advice_delivery(mode="reply", passed=True, citations=0)

    assert not run_check(rig, {"kind": "delivery_regime", "expect": "grounded"}).passed
    assert run_check(rig, {"kind": "delivery_regime", "expect": "unverified"}).passed


def test_delivery_regime_without_any_delivery_fails(rig) -> None:
    assert not run_check(rig, {"kind": "delivery_regime", "expect": "grounded"}).passed


def test_numbers_grounded_flags_a_numeric_delivery_without_the_check(rig) -> None:
    # 直接造一条"带数字但没声明 numbers.grounded"的投递：这是契约违规。
    from jagent.contracts import EventType

    rig.emit(
        EventType.ADVICE_DELIVERED,
        {
            "advice": {"body": "最新价 17.19 元。"},
            "mode": "reply",
            "delivery": {"regime": "unverified", "checks": [], "grounds": [], "fallback": {}},
        },
    )

    result = run_check(rig, {"kind": "numbers_grounded"})

    assert not result.passed
    assert result.evidence["seqs"], "违规投递的 seq 必须可追溯"


def test_numbers_grounded_passes_when_check_is_declared(rig) -> None:
    rig.advice_delivery(mode="report", passed=True, citations=1)

    result = run_check(rig, {"kind": "numbers_grounded"})

    assert result.passed
