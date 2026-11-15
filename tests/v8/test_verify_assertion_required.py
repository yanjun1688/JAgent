"""Slice 1（D-001 / G-09）：没有断言的步骤不得静默通过。

fail-closed：缺断言 = 缺观测，理由落在 `evidence_missing`。
`checks` 是逐条断言的判定，没有断言就没有条目——不造一条假检查。
"""

from __future__ import annotations

from jagent.contracts import Step, StepOutcome
from jagent.harness.verify import Verifier


def test_step_without_assertion_fails_closed() -> None:
    step = Step(id="s1", tool="fs.read")
    outcome = StepOutcome(ok=True, observations={"content": "hello"})

    verdict = Verifier().verify(step, outcome)

    assert verdict.passed is False
    assert verdict.reason.startswith("evidence_missing")
    # checks 是"逐条断言的判定"：没有断言就没有条目，不能造一条假检查
    assert verdict.checks == ()
