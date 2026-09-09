"""PlanGuardrail — trusted validation of a DagPlan before execution.

Checks tool existence, probe legality, DAG structure, ``$step`` references,
declared-operations self-consistency, dangerous tool combinations and
max_parallel warnings. Non-trusted Planner output never reaches the Executor
without passing this guardrail.
"""

from __future__ import annotations

from harness.core.logger import agent_logger
from harness.core.planner.output_refs import parse_output_refs
from harness.models.plan import DagPlan, RequiredOperation, validate_dag_structure
from harness.storage.event_store import EventStore
from harness.tools.registry import ToolRegistry

_log = agent_logger("planner")


class PlanGuardrail:
    """Validates a DagPlan before execution — tool existence, schema, cycle, safety."""

    def __init__(self, registry: ToolRegistry, store: EventStore | None = None):
        self.registry = registry
        self.store = store

    def validate(
        self,
        plan: DagPlan,
        completed_step_ids: set[str] | None = None,
        available_step_ids: set[str] | None = None,
    ) -> list[str]:
        errors = []
        completed = completed_step_ids or set()
        # Steps whose recorded output is available for $var.field references
        # even though they are not scheduled in this plan (e.g. prior
        # UNSUCCESSFUL steps). Such dependencies are valid — the upstream builder
        # resolves them via is_done, and the DAG topology treats them as
        # external (no scheduling edge).
        available = available_step_ids or set()

        if not plan.steps:
            return []

        for i, step in enumerate(plan.steps):
            if not step.id:
                errors.append(f"Step {i} is missing 'id' field")
                continue

            tool_def, unknown_error = self.registry.tool_def_or_error(step.tool)
            if tool_def is None:
                # collect-all, not fail-fast: append and keep scanning so one LLM
                # retry sees every bad step at once (R7 primitive, shared message).
                errors.append(f"Step '{step.id}': {unknown_error}")
                continue

            # v2.2 (D10): probe 信任校验 — 仅无副作用（只读/查询）工具可标 probe，
            # 否则弱模型会用它逃完成门。PlanGuardrail 是受信组件，双路径强制。
            # S02: 校验下沉到 operation 级 — file_op(read) / http_request(GET) 的
            # 只读探测不再继承工具级写/删/外部副作用。
            op_contract = tool_def.resolve_operation(step.input)
            if step.probe:
                if op_contract is not None:
                    probe_rejected = bool(op_contract.side_effects) or not op_contract.probe_allowed
                    effects = [s.value for s in op_contract.side_effects]
                    declared_for = f"operation '{op_contract.operation}'"
                else:
                    probe_rejected = bool(tool_def.side_effects)
                    effects = [s.value for s in tool_def.side_effects]
                    declared_for = f"tool '{step.tool}'"
                if probe_rejected:
                    errors.append(
                        f"Step '{step.id}': probe declaration is only allowed for "
                        f"side-effect-free (read-only/query) operations; {declared_for} "
                        f"declares side_effects={effects}"
                    )

            if not isinstance(step.input, dict):
                errors.append(f"Step '{step.id}': 'input' must be an object")
                continue

        if errors:
            return errors

        # S03: 结构校验（纯函数）— step_id 唯一、依赖存在/自依赖、环检测（含路径）、
        # 层级一致性、input 结构。非法 DAG 在 Executor 之前被受信 PlanGuardrail 拒绝，
        # 不依赖 topological_sort 的运行时 ValueError。
        errors.extend(
            validate_dag_structure(
                plan,
                completed_step_ids=completed,
                available_step_ids=available,
            )
        )

        if errors:
            return errors

        # S04 (D-01 / C-04): 步骤输出引用静态校验 — 非法 $step 引用在 Executor
        # 之前被拒，防止 "$s1.result" 被当普通路径/字面量传给工具。
        errors.extend(
            parse_output_refs(
                plan,
                registry=self.registry,
                completed_step_ids=completed,
                available_step_ids=available,
            )
        )

        if errors:
            return errors

        # Q-02 (ADR-009): declared_operations 自洽检查（保留，仅作 LLM 计划结构检查，
        # 不承担交付验收）。LLM 自报的操作必须在计划中有匹配步骤，否则拒绝让 Planner 重试。
        # 结构化子集匹配（RequiredOperation.step_satisfies），不硬编码工具语义。
        if plan.declared_operations:
            for i, req in enumerate(plan.declared_operations):
                if not any(RequiredOperation.step_satisfies(s, req) for s in plan.steps):
                    errors.append(
                        f"Declared operation #{i} ({req.tool} {req.input}) has no matching step in the plan. "
                        "This is a self-check declaration — keep the plan self-consistent."
                    )

        if errors:
            return errors

        errors.extend(self._check_dangerous_combinations(plan))
        errors.extend(self._check_max_parallel(plan))

        return errors

    def _check_dangerous_combinations(self, plan: DagPlan) -> list[str]:
        errors = []
        tool_names = {s.tool for s in plan.steps}
        for step in plan.steps:
            tool_def = self.registry.get_tool_def(step.tool)
            if tool_def and tool_def.dangerous_with:
                for dangerous in tool_def.dangerous_with:
                    if dangerous in tool_names:
                        errors.append(
                            f"Dangerous combination: '{step.tool}' and '{dangerous}' cannot appear in the same plan"
                        )
        return errors

    def _check_max_parallel(self, plan: DagPlan) -> list[str]:
        """Warn when max_parallel exceeded — enforcement is via DagExecutor semaphore."""
        step_map = {s.id: s for s in plan.steps}
        try:
            layers = plan.topological_sort()
        except ValueError:
            return []
        for layer in layers:
            reported = set()
            for sid in layer:
                step = step_map.get(sid)
                if not step or step.tool in reported:
                    continue
                limit = step.max_parallel
                tool_def = self.registry.get_tool_def(step.tool)
                if tool_def:
                    limit = min(limit, tool_def.max_parallel)
                count_in_layer = sum(1 for s in layer if step_map.get(s) and step_map[s].tool == step.tool)
                if count_in_layer > limit:
                    _log.warning(
                        "Tool '%s' appears %d times in one layer (max_parallel=%d) — relying on semaphore",
                        step.tool,
                        count_in_layer,
                        limit,
                    )
                reported.add(step.tool)
        return []
