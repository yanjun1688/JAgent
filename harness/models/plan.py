from __future__ import annotations

from collections import deque
from typing import Any

from pydantic import BaseModel, Field

# Bug B / P1-13 13.4: workspace 根路径的人类可读别名，契约抽取可能写出它们，
# 而 Planner 实际使用真实路径 "."。仅用于 list 操作的语义归一化判定。
_WORKSPACE_ROOT_ALIASES: frozenset[str] = frozenset(
    {".", "./", "workspace", "workspace directory", "current directory", "the workspace", "/workspace", "root"}
)


class DagStep(BaseModel):
    id: str = ""
    tool: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    description: str = ""
    upstream_selectors: dict[str, str] | None = None
    max_parallel: int = 10
    branches: dict | None = None
    probe: bool = False


class OutputRef(BaseModel):
    """S04 (D-01): structured step-output reference.

    The LLM keeps writing ``$s1.result``; the trusted PlanGuardrail parses it
    into this model for static validation, and the trusted Executor only
    resolves already-validated references (via ``dag_vars``). ``field_path``
    is empty for a whole-output reference (``$s1``).
    """

    step_id: str = ""
    field_path: str = ""


class RequiredOperation(BaseModel):
    """LLM 自检声明（Q-02，类名保留为匹配逻辑载体，不代表系统要求）。

    它是 LLM 计划中"自报应覆盖的操作"（``DagPlan.declared_operations``），
    属于非受信组件输出，仅用于：LLM 计划结构自洽检查、修复反馈、审计。
    不可用于：创建交付契约、授权新副作用、替代 DeliveryContract、决定最终完成
    （ADR-009 Q-02）。真正被系统强制的是 ``DeliveryContract``。
    ``input`` 只需声明判定性键值（如 file_op 的 operation/path），
    与 step 的匹配规则：tool 相同 且 step.input 包含所有声明键值。
    """

    tool: str = ""
    input: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def _paths_equivalent(path_a: str, path_b: str, operation: str | None) -> bool:
        """Workspace 根路径语义归一化（Bug B / P1-13 13.4）。

        契约抽取（LLM）可能把 workspace 根写成人类可读别名（如
        ``"workspace directory"``），而 Planner 实际使用真实路径 ``"."``。
        两者语义等价，必须判定 met，否则成功执行会被误报 ``Deliverable not met``。
        归一化只应用于 list/read/write 等对 workspace 根合法的操作；对明确的
        子路径/越界路径不做任何等价（保持安全，不扩大匹配面）。
        """
        if path_a == path_b:
            return True
        if operation != "list":
            return False
        norm_a = path_a.strip().lower().rstrip("/") or "."
        norm_b = path_b.strip().lower().rstrip("/") or "."
        if norm_a in _WORKSPACE_ROOT_ALIASES:
            return norm_b in _WORKSPACE_ROOT_ALIASES
        return False

    @staticmethod
    def step_satisfies(step: DagStep, req: "RequiredOperation") -> bool:
        """D-03 structural match; content is stored but not a gate key (L-02)."""
        if step.tool != req.tool:
            return False
        for key, value in req.input.items():
            if key == "content":
                continue
            if key == "path":
                step_value = step.input.get(key)
                if not RequiredOperation._paths_equivalent(
                    str(value), str(step_value) if step_value is not None else "", req.input.get("operation")
                ):
                    return False
            elif step.input.get(key) != value:
                return False
        return True


def _find_cycle(steps: list[DagStep], step_ids: set[str]) -> list[str]:
    """Detect a cycle in the in-plan dependency graph (DFS three-colour).

    Returns the first cycle as a node path (``["s1", "s3", "s1"]``) or an empty
    list when the graph is acyclic.  External dependencies (completed/available)
    are not part of the graph and never create edges.
    """
    adjacency: dict[str, list[str]] = {}
    for step in steps:
        adjacency[step.id] = [d for d in step.depends_on if d in step_ids]
    white, gray, black = 0, 1, 2
    color: dict[str, int] = {sid: white for sid in step_ids}
    stack: list[str] = []

    def _dfs(sid: str) -> list[str] | None:
        color[sid] = gray
        stack.append(sid)
        for nxt in adjacency.get(sid, []):
            if color[nxt] == gray:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color[nxt] == white:
                result = _dfs(nxt)
                if result is not None:
                    return result
        stack.pop()
        color[sid] = black
        return None

    for sid in step_ids:
        if color[sid] == white:
            result = _dfs(sid)
            if result is not None:
                return result
    return []


def _hierarchy_errors(steps: list[DagStep], step_ids: set[str]) -> list[str]:
    """Verify that every step's level == max(in-plan dependency level) + 1.

    A step with a dependency must never be schedulable before that dependency
    (i.e. its level must be strictly greater than each in-plan dependency's
    level).  External dependencies are ignored (their level is unknown).
    """
    step_map = {s.id: s for s in steps}
    levels: dict[str, int] = {}
    errors: list[str] = []

    def _level(sid: str) -> int:
        if sid in levels:
            return levels[sid]
        step = step_map.get(sid)
        if step is None:
            return 0
        dep_levels = [_level(d) for d in step.depends_on if d in step_ids]
        level = (max(dep_levels) + 1) if dep_levels else 0
        levels[sid] = level
        return level

    for step in steps:
        level = _level(step.id)
        in_plan_deps = [d for d in step.depends_on if d in step_ids]
        if not in_plan_deps:
            continue
        for dep in in_plan_deps:
            if level <= levels.get(dep, 0):
                errors.append(
                    f"Step '{step.id}' is scheduled before its dependency '{dep}' "
                    f"(hierarchy inconsistency: dep level {levels.get(dep, 0)} >= {level})"
                )
    return errors


def validate_dag_structure(
    plan: "DagPlan",
    completed_step_ids: set[str] | None = None,
    available_step_ids: set[str] | None = None,
) -> list[str]:
    """Pure DAG structure validation (S03).

    Checks — returns a list of error messages; empty list = valid.  Never
    raises ValueError (the executor's ``topological_sort`` runtime error is
    NOT used as a detection mechanism; this runs in the trusted PlanGuardrail
    before any execution):

      1. step_id uniqueness
      2. depends_on references exist in current plan ∪ completed ∪ available
      3. self-dependency
      4. cycle detection (DFS, message includes the cycle path)
      5. hierarchy consistency (a step with dependencies is scheduled after them)
      6. input is a dict (structural only — tool schema validation belongs to
         the Executor SchemaGuardrail)
    """
    errors: list[str] = []
    completed = set(completed_step_ids or ())
    available = set(available_step_ids or ())
    external = completed | available

    if not plan.steps:
        return errors

    # 1. step_id uniqueness (O(n), not O(n²))
    step_ids: set[str] = set()
    for i, step in enumerate(plan.steps):
        if not step.id:
            errors.append(f"Step {i} is missing 'id' field")
            continue
        if step.id in step_ids:
            errors.append(f"Duplicate step id '{step.id}'")
        else:
            step_ids.add(step.id)

    # 2+3. depends_on existence / self-dependency
    for step in plan.steps:
        if not step.id:
            continue
        for dep in step.depends_on:
            if dep == step.id:
                errors.append(f"Step '{step.id}' depends on itself")
            elif dep not in step_ids and dep not in external:
                errors.append(f"Step '{step.id}': depends on unknown step '{dep}'")

    # 4. cycle detection — only when existence checks pass to avoid noise
    if not errors:
        cycle = _find_cycle(plan.steps, step_ids)
        if cycle:
            errors.append(f"Cycle detected: {' -> '.join(cycle)}")
        else:
            # 5. hierarchy consistency (skipped when a cycle already exists)
            errors.extend(_hierarchy_errors(plan.steps, step_ids))

    # 6. input structure
    for step in plan.steps:
        if step.id and not isinstance(step.input, dict):
            errors.append(f"Step '{step.id}': 'input' must be an object")

    return errors


class DagPlan(BaseModel):
    user_intent: str = ""
    intent: str = ""
    steps: list[DagStep] = Field(default_factory=list)
    failed: bool = False
    step_tasks: dict[str, str] = Field(default_factory=dict)
    # Q-02: LLM 自检声明（非受信）。只用于计划结构自洽检查 / 修复反馈 / 审计，
    # 不参与交付验收、不授权副作用、不替代 DeliveryContract。
    declared_operations: list[RequiredOperation] = Field(default_factory=list)

    def _step_map(self) -> dict[str, DagStep]:
        return {s.id: s for s in self.steps}

    def topological_sort(
        self,
        completed_step_ids: set[str] | None = None,
        external_deps: set[str] | None = None,
    ) -> list[list[str]]:
        steps = self._step_map()
        in_degree: dict[str, int] = {}
        adjacency: dict[str, list[str]] = {}
        completed = set(completed_step_ids or ())
        # Steps from prior execution whose output is already available (incl.
        # UNSUCCESSFUL) but that are not scheduled as steps in this plan. They
        # are valid dependency targets and need no scheduling edge.
        external = set(external_deps or ())

        all_valid = set(steps.keys()) | completed | external

        for s in self.steps:
            in_degree[s.id] = 0
            adjacency[s.id] = []

        for s in self.steps:
            for dep in s.depends_on:
                if dep not in all_valid:
                    raise ValueError(f"Step '{s.id}': depends on unknown step '{dep}'")
                # External deps are by definition not in `steps`, so the
                # `dep not in steps` branch already skips their edge. A dep
                # that IS an in-plan step stays scheduled even if it also
                # appears in external (e.g. a soft-error step being re-run).
                if dep not in steps or dep in completed:
                    continue
                adjacency.setdefault(dep, []).append(s.id)
                in_degree[s.id] = in_degree.get(s.id, 0) + 1

        layers: list[list[str]] = []
        queue = deque([sid for sid, deg in in_degree.items() if deg == 0 and sid not in completed])

        visited = len(completed & set(steps.keys()))
        while queue:
            layer = []
            for _ in range(len(queue)):
                sid = queue.popleft()
                if sid in completed:
                    continue
                layer.append(sid)
                visited += 1
                for neighbor in adjacency.get(sid, []):
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0 and neighbor not in completed:
                        queue.append(neighbor)
            layers.append(layer)

        if visited != len(self.steps):
            raise ValueError("Cycle detected in DAG plan")

        return layers

    def upstream_outputs(
        self,
        step_id: str,
        results: dict[str, Any],
    ) -> dict[str, Any]:
        step = self._step_map().get(step_id)
        if not step:
            return {}
        merged: dict[str, Any] = {}
        for dep_id in step.depends_on:
            dep_result = results.get(dep_id)
            if dep_result is None:
                merged[dep_id] = None
                continue
            if isinstance(dep_result, dict):
                output = dep_result.get("output")
            elif hasattr(dep_result, "output"):
                output = dep_result.output
            else:
                output = dep_result
            selectors = step.upstream_selectors or {}
            if dep_id in selectors:
                merged[dep_id] = self._resolve_path(output, selectors[dep_id])
            else:
                merged[dep_id] = output
        return merged

    @staticmethod
    def _resolve_path(obj: Any, path: str) -> Any:
        parts = path.split(".")
        current = obj
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, (list, tuple)) and part.isdigit():
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            else:
                return None
        return current
