"""P1a 修复切片（tests/v8_repair）的共用夹具。

只装配公开契约（contracts / wiring / registry），不复制生产逻辑：
一个计数 `probe` 工具 + 一份断言失败 / 一份修好后的计划。
"""

from __future__ import annotations

from typing import Any

from jagent.contracts import (
    Budget,
    Idempotency,
    Plan,
    SideEffect,
    Step,
    ToolResult,
    ToolSpec,
    Verify,
)
from jagent.wiring import build_app


def probe_plan(*, value: Any = 1, verify: str = "value == 2") -> Plan:
    """单步 probe 计划：默认产出的 `value` 是 1，而断言要求 2（必然断言失败）。"""

    return Plan(
        steps=(
            Step(
                id="s1",
                tool="probe",
                args={"value": value},
                verify=Verify(verify, needs=("value",)),
            ),
        )
    )


def fixed_plan(*, value: Any = 2, verify: str = "value == 2") -> Plan:
    """把 args 改成满足断言的样子（模拟模型给的修订计划）。"""

    return Plan(
        steps=(
            Step(
                id="s1",
                tool="probe",
                args={"value": value},
                verify=Verify(verify, needs=("value",)),
            ),
        )
    )


def add_probe(app: Any, *, stderr: str = "") -> list[dict[str, Any]]:
    """注册一个计数 probe 工具，返回它每次被调用时收到的 args。"""

    calls: list[dict[str, Any]] = []

    def fn(args: dict[str, Any], workspace: Any, timeout: float) -> ToolResult:
        calls.append(dict(args))
        # value 直接回显 args：断言失败 / 修复后通过都由计划决定，工具本身不判。
        return ToolResult(ok=True, data={"value": args.get("value")}, stderr=stderr)

    app.registry.add(
        ToolSpec(
            name="probe",
            version="1",
            args_schema={"type": "object", "properties": {"value": {}}},
            side_effect=SideEffect.NONE,
            idempotency=Idempotency.NATURAL,
            observations=("ok", "value"),
            timeout_s=5.0,
        ),
        fn,
    )
    return calls


def build_probe_app(root: Any, *, repairer: Any, budget: Budget | None = None, stderr: str = ""):
    """build_app 的最低装配：显式 repairer + probe 工具。"""

    app = build_app(root, repairer=repairer, budget=budget or Budget(max_repairs=2))
    calls = add_probe(app, stderr=stderr)
    return app, calls
