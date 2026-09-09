"""S08 / ADR-009: revision invariants — trusted, pure enforcement.

A revised plan must not weaken the user's hard deliverables. Enforcement lives
in the trusted Scheduler side; it does not rely on the Reviser's compliance.
"""

from __future__ import annotations

from harness.models.intent import DeliveryContract, DeliverySource
from harness.models.plan import DagPlan, DagStep, RequiredOperation
from harness.tools.registry import ToolRegistry


def step_is_mutating(step: DagStep, registry: ToolRegistry) -> bool:
    """S08 (C-02): 判断 step 是否 mutating — operation 级副作用（S02 契约判定）。

    Fail-closed（v3.4 review）：未注册的工具 → 判为 mutating（True）。无法证明
    无副作用 ⇒ 不得假设无害；与 ``recovery._is_read_only_action`` 的未知工具默认
    （非只读）对齐，否则 Q-06 反向覆盖会对未知工具步骤静默失效。
    """
    tool_def = registry.get_tool_def(step.tool)
    if tool_def is None:
        return True
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
        # DeliveryContract 与 RequiredOperation 同为 {tool, input} 结构（C-01 收敛后
        # 前者是后者的受信超集，还带 contract_id/source）；step_satisfies 只读这两个
        # 属性，故传入契约在运行时安全。类型收窄（Protocol/Union）属独立 mypy 清理
        # 任务，此处行内豁免并记录，不用文件级宽豁免掩盖。
        matching = [
            s for s in revised.steps if RequiredOperation.step_satisfies(s, contract)  # type: ignore[arg-type]
        ]
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
    # 未知工具 fail-closed：step_is_mutating 对未注册工具返回 True（视为 mutating），
    # 故未覆盖的未知工具步骤在此被拒，不依赖下游 PlanGuardrail 的顺序兜底。
    if root_contracts and registry is not None:
        for step in revised.steps:
            if not step_is_mutating(step, registry):
                continue
            covered = any(
                RequiredOperation.step_satisfies(step, c)  # type: ignore[arg-type]
                for c in root_contracts
            )
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
