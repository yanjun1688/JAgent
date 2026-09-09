"""Parsing of LLM JSON responses into typed plans / repair proposals.

Non-trusted: output is raw LLM text; all results are re-validated by the
trusted PlanGuardrail / Scheduler before use.
"""

from __future__ import annotations

from harness.core.dag_types import TaskState
from harness.core.lenient_json import LenientJsonError, parse_lenient_json
from harness.core.logger import agent_logger
from harness.core.planner.schema_contract import validate_step
from harness.core.recovery import LocalRepairProposal
from harness.models.plan import DagPlan, DagStep, RequiredOperation

_log = agent_logger("planner")


def parse_local_repair_proposal(response: str) -> LocalRepairProposal | None:
    """Parse the LLM's single-action repair JSON into a typed proposal.

    Empty tool_name (``{"tool_name": ""}``) or a missing tool means "no
    repair possible" → None (escalate). Extra keys are ignored.
    """
    try:
        data = parse_lenient_json(response)
    except LenientJsonError:
        return None
    if not isinstance(data, dict):
        return None
    tool_name = str(data.get("tool_name") or "").strip()
    if not tool_name:
        return None
    raw_input = data.get("input")
    if raw_input is None:
        raw_input = data.get("parameters")
    if not isinstance(raw_input, dict):
        return None
    step_id = str(data.get("step_id") or "")
    return LocalRepairProposal(step_id=step_id, tool_name=tool_name, input=raw_input)


def parse_plan_response(
    response: str, executed_step_ids: set[str] | None = None
) -> tuple[DagPlan | None, str]:
    """返回 (plan_or_None, error_reason)。error_reason 为空字符串表示成功。"""
    try:
        data = parse_lenient_json(response)
    except LenientJsonError as e:
        if e.kind == "no_object":
            return None, "No JSON object found in response"
        cause = e.cause
        detail = f"{cause.msg} at position {cause.pos}" if cause is not None else "unparseable"
        return None, f"JSON parse error: {detail}"

    if not isinstance(data, dict):
        return None, "Top-level value must be a JSON object with a 'steps' array"

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list):
        return None, "Missing or invalid 'steps' array"

    steps = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            return None, f"Step #{i} is not a JSON object"

        # Backward compat: if 'parameters' exists but 'input' doesn't, rename
        if "input" not in s and "parameters" in s:
            s["input"] = s.pop("parameters")
        # If both exist, remove 'parameters' (input wins)
        if "parameters" in s:
            del s["parameters"]

        err = validate_step(s, i)
        if err:
            return None, err

        step_input = s.get("input", {})
        if not isinstance(step_input, dict):
            step_input = {}

        steps.append(
            DagStep(
                id=s.get("id", ""),
                tool=s.get("tool", ""),
                input=step_input,
                depends_on=s.get("depends_on", []),
                description=s.get("description", ""),
                probe=bool(s.get("probe", False)),
            )
        )

    step_tasks: dict[str, str] = {}
    raw_tasks = data.get("step_tasks")
    if raw_tasks is not None and isinstance(raw_tasks, dict):
        valid_states = {s.value for s in TaskState}
        exec_ids = executed_step_ids or set()
        for sid, ts_str in raw_tasks.items():
            if not isinstance(sid, str) or not isinstance(ts_str, str):
                continue
            if exec_ids and sid not in exec_ids:
                _log.debug("[parse] step_tasks: ignoring unknown step %s", sid)
                continue
            if ts_str not in valid_states:
                _log.warning("[parse] step_tasks: invalid state %s for %s, defaulting to unknown", ts_str, sid)
                step_tasks[sid] = TaskState.UNKNOWN.value
                continue
            step_tasks[sid] = ts_str
        if step_tasks:
            _log.info("[parse] step_tasks from LLM: %s", [(sid, ts) for sid, ts in step_tasks.items()])

    # Q-02 (ADR-009): declared_operations —— LLM 自检声明（非受信）。
    # 仅用于计划结构自洽检查 / 修复反馈 / 审计；不创建交付契约、不授权副作用、
    # 不替代 DeliveryContract、不决定最终完成。
    declared_ops: list[RequiredOperation] = []
    raw_ops = data.get("declared_operations")
    if isinstance(raw_ops, list):
        for item in raw_ops:
            if not isinstance(item, dict):
                _log.warning("[parse] declared_operations: skipping non-object item %r", item)
                continue
            tool = item.get("tool", "")
            op_input = item.get("input")
            if not tool or not isinstance(op_input, dict):
                _log.warning("[parse] declared_operations: skipping invalid item %r", item)
                continue
            declared_ops.append(RequiredOperation(tool=tool, input=op_input))

    return DagPlan(
        intent=data.get("intent", ""),
        steps=steps,
        failed=data.get("failed", False),
        step_tasks=step_tasks,
        declared_operations=declared_ops,
    ), ""
