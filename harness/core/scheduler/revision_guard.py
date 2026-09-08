"""Trusted revision guards — degenerate-revision signature guard + plan merge.

v2.2 (E, U1): a revision that re-runs an already-failed action with the same
(tool, normalized input) and adds no new upstream step cannot possibly succeed.
Signature comparison rejects such degenerate revisions at round 2 (forcing the
LLM to change strategy) instead of round 5 (outer circuit breaker).

S08: merged revisions are also checked against delivery invariants
(:func:`harness.core.planner.validate_revision_invariants`) — a revision must
not weaken the user's hard deliverables. All logic here is trusted and
mechanical; the LLM's own task assessment (``step_tasks``) is audit-only.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from harness.core.dag_types import ExecState, StepResult, TaskState
from harness.core.logger import guard_logger
from harness.core.planner import revision_invariant_feedback, validate_revision_invariants
from harness.models.plan import DagPlan, DagStep

_sched_breaker = guard_logger("scheduler.breaker")


def _normalize_input(inp: dict[str, Any]) -> str:
    """规范化工具输入，用于退化修订守卫的签名比对。"""
    return json.dumps(inp or {}, sort_keys=True, default=str)


def step_signature(step: DagStep) -> tuple[str, str]:
    """步骤动作签名：(tool, 规范化 input)。退化修订守卫据此比对。"""
    return (step.tool, _normalize_input(step.input))


def find_degenerate_revised_steps(
    plan: DagPlan,
    results: dict[str, StepResult],
    revised: DagPlan,
) -> list[str]:
    """v2.2 (E/U1): 签名比对 — 找出修订计划中必然重复失败的步骤（退化修订）。

    A revised step is degenerate iff:
      1. its (tool, normalized input) matches a step in ``results`` that is
         FAILED or UNSUCCESSFUL (non-probe), AND
      2. its transitive dependency closure (within the revised plan) adds no
         NEW step — every closure member's signature was already in ``plan``.

    Condition 2 means: re-running the same action with the same input cannot
    possibly yield a different outcome (no new upstream data source). This
    converges U1 at round 2 (reject the degenerate revision, force the LLM
    to change strategy) instead of round 5 (outer breaker).

    Returns the ids of degenerate revised steps (empty = acceptable revision).
    """
    seen_sigs = {step_signature(s) for s in plan.steps}
    failed_sigs = {
        step_signature(s)
        for s in plan.steps
        if (
            isinstance(results.get(s.id), StepResult)
            and (
                results[s.id].exec_state == ExecState.FAILED
                or (results[s.id].exec_state == ExecState.UNSUCCESSFUL and not results[s.id].probe)
            )
        )
    }
    if not failed_sigs:
        return []

    revised_map = {s.id: s for s in revised.steps}

    def _closure(sid: str) -> set[str]:
        seen: set[str] = set()
        stack = [sid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            step = revised_map.get(cur)
            if step:
                stack.extend(d for d in step.depends_on if d in revised_map)
        return seen

    degenerate: list[str] = []
    for rs in revised.steps:
        if step_signature(rs) not in failed_sigs:
            continue
        cl = _closure(rs.id)
        cl_sigs = {step_signature(revised_map[cid]) for cid in cl}
        if cl_sigs <= seen_sigs:  # 依赖闭包无新步骤
            degenerate.append(rs.id)
    return degenerate


def degenerate_feedback(degenerate: list[str]) -> str:
    """构造告知 LLM 修订被拒绝的原因（用于下一次 revise 的 feedback）。"""
    return (
        "\n[SYSTEM REJECTION] The previous revision repeats step(s) "
        + ", ".join(degenerate)
        + " with the same tool and input that already failed, and adds no new "
        "upstream step that could change the outcome. Re-running them cannot "
        "succeed. Remove or change these steps, or add a new upstream step."
    )


def merge_revised_plan(
    root_plan: DagPlan,
    current_plan: DagPlan,
    revised: DagPlan,
    results: dict[str, StepResult],
    step_aliases: dict[str, str],
) -> DagPlan:
    """Merge a revision without losing original downstream work.

    The LLM may return only a replacement for a failed step. The trusted
    scheduler restores original downstream steps, rewrites dependencies
    through the replacement alias, and clears stale SKIPPED results.
    """
    root_steps = {step.id: step for step in root_plan.steps}
    revised_steps = {step.id: step for step in revised.steps}
    current_ids = {step.id for step in current_plan.steps}
    unresolved = [
        step.id
        for step in root_plan.steps
        if not (
            isinstance(results.get(step_aliases.get(step.id, step.id)), StepResult)
            and results[step_aliases.get(step.id, step.id)].step_normal
        )
    ]
    replacement_ids = [sid for sid in revised_steps if sid not in root_steps and sid not in current_ids]
    # Prefer a unique exact signature match for every unresolved step. This
    # prevents LLM output ordering from deciding which failed step a
    # replacement satisfies. Signature matching is intentionally exact:
    # aliases such as HTTP ``method``/``action`` are not normalized here
    # because guessing tool equivalence could merge side-effecting actions.
    unmatched_replacements = set(replacement_ids)
    unmatched_originals: list[str] = []
    for original_id in unresolved:
        prior = results.get(step_aliases.get(original_id, original_id))
        if not isinstance(prior, StepResult) or prior.exec_state not in (
            ExecState.FAILED,
            ExecState.UNSUCCESSFUL,
            ExecState.SKIPPED,
        ):
            continue
        original_step = root_steps[original_id]
        matches = [
            sid
            for sid in unmatched_replacements
            if step_signature(revised_steps[sid]) == step_signature(original_step)
        ]
        if len(matches) == 1:
            step_aliases[original_id] = matches[0]
            unmatched_replacements.remove(matches[0])
        else:
            unmatched_originals.append(original_id)

    # D12 unambiguous 1:1 binding (replaces D12 "B replacement" in a serial
    # chain): when after signature matching exactly ONE replacement and
    # exactly ONE ran-and-failed (FAILED/UNSUCCESSFUL, NOT SKIPPED) original
    # step remain, the mapping is forced — bind them even though the input
    # changed. SKIPPED steps are excluded: they never ran, so they are
    # restored instead of replaced. This is NOT positional guesswork: with
    # 2+ candidates on either side the binding is refused (fail-safe, M4).
    ran_and_failed = [
        sid
        for sid in unmatched_originals
        if results[step_aliases.get(sid, sid)].exec_state in (ExecState.FAILED, ExecState.UNSUCCESSFUL)
    ]
    if len(unmatched_replacements) == 1 and len(ran_and_failed) == 1:
        replacement_id = next(iter(unmatched_replacements))
        step_aliases[ran_and_failed[0]] = replacement_id
        unmatched_replacements.discard(replacement_id)

    merged: dict[str, DagStep] = dict(revised_steps)
    for original_id, original_step in root_steps.items():
        alias = step_aliases.get(original_id, original_id)
        if alias != original_id or original_id in revised_steps:
            continue
        prior = results.get(original_id)
        if isinstance(prior, StepResult) and prior.step_normal:
            continue
        rewritten_deps = [step_aliases.get(dep, dep) for dep in original_step.depends_on]
        merged[original_id] = original_step.model_copy(update={"depends_on": rewritten_deps})

    for sid in list(merged):
        prior = results.get(sid)
        if isinstance(prior, StepResult) and not prior.step_normal:
            results.pop(sid, None)
    for original_id, alias in step_aliases.items():
        if alias != original_id:
            # The original result is no longer a canonical dependency.
            # Leaving it available would let a later revision consume a
            # stale UNSUCCESSFUL output through external_deps.
            results.pop(original_id, None)

    return revised.model_copy(update={"steps": list(merged.values())})


def merge_step_tasks(results: dict[str, StepResult], revised: DagPlan) -> int:
    """Merge the LLM's step_tasks annotations into results.

    v2.1 (Bug S1.1): purely observational. task_state does NOT affect any
    scheduling decision — should_not_rerun / step_normal are pure ExecState
    functions that never read it (AGENTS.md constraint 4).
    v2.2 (D11): task_state remains an audit-only note; it is persisted in
    PlanRevisedPayload for audit and future "LLM self-judgment vs system
    mechanical judgment" comparison. It NEVER participates in any trusted
    decision (constraint 4).

    Returns the number of annotations merged.
    """
    merged = 0
    if not revised.step_tasks:
        return 0
    for sid, ts_str in revised.step_tasks.items():
        if sid in results:
            try:
                results[sid].task_state = TaskState(ts_str)
                merged += 1
            except ValueError:
                pass
    return merged


class RevisionGuardMixin:
    """Degenerate-revision guard + delivery-invariant guard on revise (trusted)."""

    @staticmethod
    def _find_degenerate_revised_steps(
        plan: DagPlan,
        results: dict[str, StepResult],
        revised: DagPlan,
    ) -> list[str]:
        return find_degenerate_revised_steps(plan, results, revised)

    @staticmethod
    def _degenerate_feedback(degenerate: list[str]) -> str:
        return degenerate_feedback(degenerate)

    @staticmethod
    def _merge_revised_plan(
        root_plan: DagPlan,
        current_plan: DagPlan,
        revised: DagPlan,
        results: dict[str, StepResult],
        step_aliases: dict[str, str],
    ) -> DagPlan:
        return merge_revised_plan(root_plan, current_plan, revised, results, step_aliases)

    @staticmethod
    def _merge_step_tasks(results: dict[str, StepResult], revised: DagPlan) -> int:
        return merge_step_tasks(results, revised)

    async def _revise_with_degenerate_guard(
        self,
        run_id: str,
        plan: DagPlan,
        results: dict[str, StepResult],
        sys_state: str,
        feedback: str | None,
        intent_fallback: str,
        root_contracts: list[Any] | None = None,
        intent_raw: str = "",
        merge_context: tuple[DagPlan, DagPlan, dict[str, str]] | None = None,
    ) -> tuple[DagPlan | None, str | None]:
        """planner.revise 包装 E 阶段退化修订守卫（U1）+ S08 交付不变量守卫。

        每次 revise 后：
          - 退化守卫：签名比对（拒绝"重复已失败动作、依赖闭包无新步骤"的修订）；
          - S08 不变量：合并原始步骤后校验交付契约未被弱化（validate_revision_invariants）。
        任一项失败 → 拒绝并重试（上限 config.max_revise_retries）。

        Returns (revised_or_None, error_or_None)。error 非空表示重试预算已耗尽，
        调用方应以该消息 fail run。merge_context=(root_plan, current_plan, step_aliases)
        时在守卫内先做修订合并（合并后校验），调用方不再二次合并。
        """
        fb = feedback
        degenerate: list[str] = []
        invariant_errors: list[str] = []
        for attempt in range(self.config.max_revise_retries + 1):
            revised = await self._phase_call(
                run_id,
                "revise",
                self.planner.revise(
                    plan,
                    results,
                    sys_state,
                    feedback=fb,
                    intent_fallback=intent_fallback,
                    run_id=run_id,
                ),
            )
            if revised is None:
                return None, None
            degenerate = self._find_degenerate_revised_steps(plan, results, revised)

            # S08: 不变量校验在"合并后"的计划上判定。用深拷贝合并，避免重试循环中
            # 真实 results/step_aliases 被 _merge_revised_plan 变异（导致退化检测漂移）。
            invariant_errors = []
            if revised.steps and merge_context is not None and not degenerate:
                root_plan, current_plan, step_aliases = merge_context
                check_results = copy.deepcopy(results)
                check_aliases = dict(step_aliases)
                merged_check = self._merge_revised_plan(
                    root_plan, current_plan, revised, check_results, check_aliases
                )
                invariant_errors = validate_revision_invariants(
                    list(root_contracts or ()),
                    intent_raw,
                    merged_check,
                    registry=self.planner.registry,
                )

            if not degenerate and not invariant_errors:
                return revised, None

            if degenerate:
                _sched_breaker.error(
                    "[breaker] Degenerate revision rejected (attempt %d/%d): step(s) %s "
                    "repeat a failed action (same tool+input, no new dep)",
                    attempt + 1,
                    self.config.max_revise_retries + 1,
                    ", ".join(degenerate),
                )
                fb = (fb or "") + self._degenerate_feedback(degenerate)
            if invariant_errors:
                _sched_breaker.error(
                    "[breaker] Revision invariant rejected (attempt %d/%d): %s",
                    attempt + 1,
                    self.config.max_revise_retries + 1,
                    "; ".join(invariant_errors),
                )
                fb = (fb or "") + revision_invariant_feedback(invariant_errors)
        _sched_breaker.error("[breaker] Degenerate revision budget exhausted for run=%s", run_id)
        return None, (
            f"Degenerate self-heal: step(s) {', '.join(degenerate)} kept repeating a "
            f"failed action (same tool+input, no new dependency) — not converging"
            + (f"; invariant violations: {'; '.join(invariant_errors)}" if invariant_errors else "")
        )
