"""JAgent v8 · 工具契约快照（扩展点，未接线）。

为什么留这个扩展点
------------------
计划一旦冻结，`plan_hash` 只能证明「参数没变」，证明不了「工具还是那个工具」。
如果冻结之后 registry 里的 `fs.write` 被换成另一个实现（插件重载、测试注入、
热更新），同一个计划会以不同语义执行，回放也不再可信。

v6 的 `kernel/bundle.py` 已经走过这条思路：把「这个计划被哪些契约冻结」当成
数据写进 `PlanFrozen`，而不是留在进程的环境配置里。这里只保留 v8 需要的一半：
对一组 `ToolSpec` 做稳定哈希，并在派发前复核。v6 的版本清单/兼容矩阵
（`check_compat`）没有搬过来——v8 目前只有一个 bundle，没有跨版本协商。

哈希覆盖「会改变一次调用含义」的字段；`description` 是散文，不参与。
`observations` 必须参与：planner 的 `needs ⊆ provides` 依赖它，观测面变了，
旧断言会悄悄退化成 evidence_missing。

Not wired yet:
* 冻结：`jagent/app/runner.py:73-77` 写 `PLAN_FROZEN` 时，payload 里加
  `"tool_snapshot": snapshot(runner.registry.specs())`（registry 需要补一个
  返回 `ToolSpec` 迭代器的方法）。
* 派发：`jagent/harness/executor.py:138-141` 在 `registry.call(...)` 之前用
  `verify(frozen_snapshot, current_specs)` 复核；False 时返回
  `ToolResult(ok=False, error_code="tool_contract_changed")`，不要继续执行。
  冻结值随 run 的事件流保存，重放/恢复时从 `PLAN_FROZEN` 读，别读进程内缓存。

接线后的形态大致是::

    frozen = snapshot(registry.specs())          # 与 PLAN_FROZEN 一起落盘
    if not verify(frozen, registry.specs()):     # 每次派发前
        return ToolResult(ok=False, error_code="tool_contract_changed")

不做的事：不负责跨 bundle 的版本协商（v6 的 `check_compat`）、不落盘、不比较
执行环境或模型版本。它只回答一个问题：这组声明还是不是刚才那组。

已知取舍：`timeout_s` 参与哈希，因为它决定一次调用会不会被 executor 中断；
但执行器本次传入的 timeout 参数不参与——那是调用参数，不是工具契约。
`args_schema` 按原样进入 `digest_of`；schema 里如果有非 JSON 值，快照会抛
`TypeError`，这是有意的：冻结一个编码不了的契约，重放时只会更晚才炸。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..contracts import ToolSpec, digest_of

#: 加字段就要改版本前缀：旧快照不能被当成新语义的合法值（v6 的 HANDOFF #32）。
SNAPSHOT_PREFIX = "tool-contract-v1:"

__all__ = ["SNAPSHOT_PREFIX", "snapshot", "verify"]


def _contract(spec: ToolSpec) -> dict[str, Any]:
    """One declaration reduced to the fields that change what a call *means*."""
    return {
        "name": spec.name,
        "version": spec.version,
        "args_schema": dict(spec.args_schema),
        "side_effect": spec.side_effect.value,
        "idempotency": spec.idempotency.value,
        "readonly": spec.readonly,
        "timeout_s": spec.timeout_s,
        "error_codes": list(spec.error_codes),
        "observations": list(spec.observations),
    }


def snapshot(tools: Iterable[ToolSpec]) -> str:
    """Stable digest of the tool contracts a plan is about to run against.

    Sorted by name so registry insertion order is not part of the contract.
    Duplicate names raise: a registry that can hand out two `fs.write` entries
    has no single contract to freeze, and silently picking one would hide it.
    """
    contracts = [_contract(spec) for spec in tools]
    contracts.sort(key=lambda item: item["name"])
    names = [item["name"] for item in contracts]
    if len(names) != len(set(names)):
        raise ValueError("duplicate tool name in contract snapshot")
    return SNAPSHOT_PREFIX + digest_of(contracts)


def verify(snapshot_hash: str, tools: Iterable[ToolSpec]) -> bool:
    """True only when *tools* still hash to *snapshot_hash*.

    Fail-closed: a missing, foreign-prefixed or malformed snapshot is False,
    never "assume unchanged".
    """
    if not isinstance(snapshot_hash, str) or not snapshot_hash.startswith(SNAPSHOT_PREFIX):
        return False
    try:
        current = snapshot(tools)
    except (AttributeError, TypeError, ValueError):
        return False
    return current == snapshot_hash
