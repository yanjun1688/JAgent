"""S08 / ADR-009: revision invariants — trusted, pure enforcement.

A revised plan must not weaken the user's hard deliverables. Enforcement lives
in the trusted Scheduler side; it does not rely on the Reviser's compliance.
"""

from __future__ import annotations

from harness.models.intent import DeliveryContract, DeliverySource
from harness.models.plan import DagPlan, DagStep, RequiredOperation
from harness.tools.registry import ToolRegistry


def step_is_mutating(step: DagStep, registry: ToolRegistry) -> bool:
    """S08 (C-02): 判断 step 是否 mutating — operation 级副作用（S02 契约判定）。"""
    tool_def = registry.get_tool_def(step.tool)
    if tool_def is None:
        return False
    op = tool_def.resolve_operation(step.input)
    if op is not None:
        return bool(op.side_effects)
    return bool(tool_def.side_effects)


def validate_revision_invariants(
    root_contracts: list[DeliveryContract],
    intent_raw: str,
    revised: DagPlan,
    registry: ToolRegistry | None = None,
) -> list[str]:
    """S08: 校验修订计划未弱化交付目标（受信 Scheduler 侧强制，不依赖 Reviser 自觉）。

    规则：
      1. 不得删除/弱化 DeliveryContract：每条契约在修订后计划中仍有匹配 step
         （正向覆盖，复用 ``RequiredOperation.step_satisfies``）。
      2. source=caller 契约的匹配 step 不得改契约关键参数（operation/path/...）。
      3. 修订后不得引入契约未覆盖的 mutating 步骤（Q-06，C-02 反向覆盖只认 DeliveryContract，
         不认 LLM 在 ``declared_operations`` 的自报 —— 堵 self-authorize 漏洞）。
         —— 仅当运行存在契约时强制（无契约的 legacy 运行走 unverified 语义）。
         （原始 intent 的不可变由 S05 在 RunStarted 事件层保证，计划内
         intent/user_intent 只是 LLM 重述的审计字段。）
    """
    errors: list[str] = []

    for contract in root_contracts:
        matching = [s for s in revised.steps if RequiredOperation.step_satisfies(s, contract)]
        if not matching:
            errors.append(
                f"Revision removed required operation: {contract.tool} {contract.input} "
                f"(contract {contract.contract_id}) — the user's hard deliverable must be preserved"
            )
            continue
        if contract.source == DeliverySource.CALLER:
            for step in matching:
                for key, val in contract.input.items():
                    if step.input.get(key) != val:
                        errors.append(
                            f"Revision changed '{key}' for caller contract {contract.contract_id}: "
                            f"expected {val!r}, got {step.input.get(key)!r}"
                        )

    # Q-06 (ADR-009): C-02 反向覆盖只认 DeliveryContract —— mutating 步骤必须被
    # DeliveryContract 覆盖，绝不因 LLM 在 declared_operations 自报而被授权
    # （堵 self-authorize 漏洞）。仅当运行存在契约时强制（无契约 legacy 走 unverified）。
    if root_contracts and registry is not None:
        for step in revised.steps:
            if not step_is_mutating(step, registry):
                continue
            covered = any(RequiredOperation.step_satisfies(step, c) for c in root_contracts)
            if not covered:
                errors.append(
                    f"Revision introduced un-declared mutating step '{step.id}' "
                    f"({step.tool} {step.input}) not covered by any delivery contract"
                )

    return errors


def revision_invariant_feedback(errors: list[str]) -> str:
    """构造告知 LLM 修订被拒绝的反馈（下一次 revise 用）。"""
    if not errors:
        return ""
    return (
        "\n[SYSTEM REJECTION] The previous revision violated delivery invariants:\n"
        + "\n".join(f"  - {e}" for e in errors)
        + "\nRestore the user's hard deliverable operations (same tool, operation, and path). "
        "You may add helper steps, but you must NOT remove, weaken, or rewrite them."
    )
