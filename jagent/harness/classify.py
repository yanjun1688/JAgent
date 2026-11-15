"""JAgent v8 · 失败三分。

一次失败只有两种来源，分类只有三条出口：

- 工具没成功 → 查 `contracts.ERROR_CLASS` 拿到 `transient` / `deterministic`；
- 工具成功了但断言不过 → `assertion`（该改参数或改计划，不该盲目重试）；
- 两边都没失败 → `None`（没有失败可分类）。

判定顺序是「工具优先」：工具都没成功时，断言不通只是它的后果，
拿后果去决定重试策略会把瞬时故障误当成计划错误。
"""

from __future__ import annotations

from ..contracts import FailureClass, StepOutcome, VerifyOutcome, classify_error


def classify(outcome: StepOutcome, verify: VerifyOutcome) -> FailureClass | None:
    if not outcome.ok:
        return classify_error(outcome.error_code)
    if not verify.passed:
        return FailureClass.ASSERTION
    return None
