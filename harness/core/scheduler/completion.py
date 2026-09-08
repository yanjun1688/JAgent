"""S06: completion gate — dual-dimension verdict (mechanical + deliverable).

Trusted, pure functions (D-03 / D-04 / D-05 / C-02 / C-06):

- **mechanical**: every root step is ``step_normal`` plus LLM self-declared
  ``declared_operations`` coverage (Q-02; a mechanical dimension only — it does
  NOT represent delivery).
- **deliverable**: each ``DeliveryContract`` is matched to a normal step.
  Empty contracts → ``deliverable_status="unverified"``. Never fake-green.

The mixin exposes :meth:`CompletionGateMixin._completion_gate` on the scheduler
so existing call sites and tests keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from harness.core.dag_types import StepResult
from harness.models.plan import DagPlan, RequiredOperation


@dataclass
class DeliverableVerdict:
    contract_id: str
    status: Literal["met", "unmet", "unverified"]
    matched_step_ids: list[str]


@dataclass
class CompletionVerdict:
    mechanical_complete: bool
    unmet_step_ids: list[str]
    deliverables: list[DeliverableVerdict] = field(default_factory=list)
    deliverable_met: bool = False
    deliverable_status: str = "unverified"  # "met" | "unverified" | "failed"

    @classmethod
    def compute(
        cls,
        plan: DagPlan,
        results: dict[str, StepResult],
        step_aliases: dict[str, str] | None = None,
        contracts: list[Any] | None = None,
    ) -> "CompletionVerdict":
        aliases = step_aliases or {}
        unmet = [
            sid
            for sid in (s.id for s in plan.steps)
            if not (
                isinstance(results.get(aliases.get(sid, sid)), StepResult)
                and results[aliases.get(sid, sid)].step_normal
            )
        ]
        # Q-02 (ADR-009): LLM 自报 declared_operations 仍属机械维度（不依赖契约来源，
        # 不代表交付达成；交付维度只由 DeliveryContract 驱动）。
        for i, req in enumerate(plan.declared_operations):
            matching_normal = any(
                isinstance(results.get(aliases.get(s.id, s.id)), StepResult)
                and results[aliases.get(s.id, s.id)].step_normal
                for s in plan.steps
                if RequiredOperation.step_satisfies(s, req)
            )
            if not matching_normal:
                unmet.append(f"declared_op#{i}:{req.tool} {req.input}")
        mechanical_complete = len(unmet) == 0

        contracts = list(contracts or ())
        deliverables = verify_deliverables(contracts, plan, results, aliases)
        if not contracts:
            deliverable_met = False
            deliverable_status = "unverified"  # D-04
        else:
            deliverable_met = bool(deliverables) and all(v.status == "met" for v in deliverables)
            deliverable_status = "met" if deliverable_met else "failed"
        return cls(
            mechanical_complete=mechanical_complete,
            unmet_step_ids=unmet,
            deliverables=deliverables,
            deliverable_met=deliverable_met,
            deliverable_status=deliverable_status,
        )


def verify_deliverables(
    contracts: list[Any],
    plan: DagPlan,
    results: dict[str, StepResult],
    step_aliases: dict[str, str] | None = None,
) -> list[DeliverableVerdict]:
    """C-06: 受信校验器 — 只对照已存在的契约验证达成度，绝不推断用户意图。

    判定规则（D-03）：契约 tool/input 匹配 step 且该 step step_normal → met。
    匹配复用 ``RequiredOperation.step_satisfies``（结构化子集匹配）。
    """
    aliases = step_aliases or {}
    verdicts: list[DeliverableVerdict] = []
    for contract in contracts:
        matched = [
            s.id
            for s in plan.steps
            if RequiredOperation.step_satisfies(s, contract)
            and isinstance(results.get(aliases.get(s.id, s.id)), StepResult)
            and results[aliases.get(s.id, s.id)].step_normal
        ]
        verdicts.append(
            DeliverableVerdict(
                contract_id=contract.contract_id,
                status="met" if matched else "unmet",
                matched_step_ids=matched,
            )
        )
    return verdicts


class CompletionGateMixin:
    """Provides ``_completion_gate`` on the planning scheduler (trusted)."""

    @staticmethod
    def _completion_gate(
        plan: DagPlan,
        results: dict[str, StepResult],
        step_aliases: dict[str, str] | None = None,
        contracts: list[Any] | None = None,
    ) -> CompletionVerdict:
        """S06: 完成门双维判定（mechanical + deliverable）。

        v2.2 (D5/D12): 机械完成 = 全局原始步骤 step_normal 聚合 + LLM 自报
        declared_operations 达成（Q-02，机械维度，不代表交付）。S06 (D-03/D-04):
        交付维度 = DeliveryContract 逐条判定（tool/input 匹配 + step_normal）。
        空契约 → deliverable_status="unverified"。绝不宣称交付达成（C-02 fail-safe）。
        """
        return CompletionVerdict.compute(plan, results, step_aliases, contracts)
