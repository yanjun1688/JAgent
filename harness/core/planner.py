"""Planner (V0.7, L4) — generates and revises DAG Plans via LLM.

Non-trusted component. Plan output is validated by PlanGuardrail before execution.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from harness.core.dag_types import StepResult, TaskState
from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger, fmtkv
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import Episode
from harness.models.intent import DeliveryContract, DeliverySource
from harness.models.plan import (
    DagPlan,
    DagStep,
    OutputRef,
    RequiredOperation,
    validate_dag_structure,
)
from harness.storage.event_store import EventStore
from harness.tools.registry import ToolRegistry

_log = agent_logger("planner")

# ── JSON Schema 统一驱动 ───────────────────────────────────────
# Schema 定义 → prompt 生成 → 输出校验，三处使用同一来源

_STEP_SCHEMA_SIMPLE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique step id, e.g. 's1', 's2'"},
        "tool": {"type": "string", "description": "Tool name from available tools"},
        "input": {
            "type": "object",
            "description": "ALL action/url/query params go HERE, not at step level",
            "additionalProperties": True,
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs of steps this step depends on (empty if independent)",
        },
        "description": {"type": "string", "description": "What this step does"},
        "probe": {
            "type": "boolean",
            "description": "v2.2 (D4): 探测型步骤声明 — 目标是'查清楚'，答案'没有/不存在'就是正确答案。"
            "仅无副作用（只读/查询）工具步骤可标，否则计划被系统拒绝。"
            "声明 probe 后，该步骤 UNSUCCESSFUL 算正常（step_normal=True）。",
        },
    },
    "required": ["id", "tool", "input"],
    "additionalProperties": False,
}


def _build_step_schema_text() -> str:
    """Return LLM-readable schema text with single braces (for direct use)."""
    return """Top-level JSON MUST contain:
  - "intent" (string, required): a one-sentence summary of what this plan aims to accomplish.
    Rephrase the user's goal in your own words — DO NOT copy-paste the user intent verbatim.
  - "steps" (array): list of step objects. Use [] for no-action plans.
  - "declared_operations" (array of objects, recommended): the operations YOU declare this plan
    covers — a LLM self-check declaration, NOT the user's delivery contract. Each item is
    {"tool": "<tool>", "input": {key: value}} declaring a covered operation
    (e.g. {"tool": "file_op", "input": {"operation": "write", "path": "x.txt"}}).
    The system uses it ONLY to check that your plan is self-consistent; it never authorizes
    new side effects and never decides completion. Real delivery requirements are enforced
    by the trusted delivery contracts. Do NOT omit operations the user explicitly requested
    (creating/writing/deleting files, fetching URLs, etc.).

Each step MUST be a JSON object with exactly these fields:
  - "id" (string, required): unique identifier, e.g. "s1"
  - "tool" (string, required): tool name from the available tools list
  - "input" (object, required): ALL parameters go inside this object.
    NEVER put parameters like 'action', 'url', 'query' at the step level.
    Good: {"id": "s1", "tool": "http_request", "input": {"action": "GET", "url": "..."}}
    Bad:  {"id": "s1", "tool": "http_request", "action": "GET", "url": "..."}
  - "depends_on" (array of strings, optional): step dependencies for DAG ordering
  - "description" (string, optional): what this step does
  - "probe" (boolean, optional, v2.2): set true ONLY when the step's goal is to
    CHECK something and "not found / does not exist" IS the correct answer
    (e.g. verifying an endpoint, checking a flag). Only read-only / query tools
    may be marked probe — the system rejects plans that mark a mutating tool as
    probe. A probe step that returns "not found" counts as normally completed.

No other fields are allowed at the step level."""


def _validate_step(step: dict, step_index: int) -> str | None:
    """验证单个 step 是否符合 schema，返回错误描述（None 表示通过）。"""
    import jsonschema
    from jsonschema import ValidationError

    try:
        jsonschema.validate(instance=step, schema=_STEP_SCHEMA_SIMPLE)
    except ValidationError as e:
        bad_field = ".".join(str(p) for p in e.path) if e.path else "structure"
        return (
            f"Step '{step.get('id', f'#{step_index}')}' has an error: "
            f"field '{bad_field}': {e.message}. "
            f"Remember: ALL tool parameters must be inside 'input'."
        )
    return None


# Pre-compute for _retry_prompt (single braces, no .format())
_STEP_SCHEMA_RAW = _build_step_schema_text()

# ── Retry prompt (dynamic, not a template) ────────────────────


def _retry_prompt(last_error: str) -> str:
    """生成带具体错误信息的重试提示。"""
    return (
        f"Your previous response had a format error:\n{last_error}\n\n"
        f"Please fix this and output ONLY valid JSON.\n"
        f"Remember the required format:\n{_STEP_SCHEMA_RAW}"
    )


# ── S04: 步骤输出引用静态校验（D-01 / C-04）──────────────────────

_REF_PURE_PATTERN = re.compile(r"^\$([A-Za-z_][\w-]*)(?:\.([\w.]+))?$")
_REF_INLINE_PATTERN = re.compile(r"\$([A-Za-z_][\w-]*)(?:\.([\w.]+))?")


def _is_plausible_step_id(sid: str) -> bool:
    """All-numeric names (``$100``) are money/literal text, never step ids."""
    return bool(sid) and not sid.isdigit()


def _schema_has_field(output_schema: dict | None, field_path: str) -> bool:
    """Shallow top-level property check (S04 §6): lenient by design.

    Allows unknown fields when the schema declares ``additionalProperties``,
    a ``*`` wildcard property, OR no explicit ``properties`` at all (an
    unconstrained object).  Only schemas that explicitly enumerate
    ``properties`` reject a missing field.
    """
    schema = output_schema or {}
    if schema.get("additionalProperties"):
        return True
    props = schema.get("properties")
    if not props:
        return True
    if "*" in props:
        return True
    if not field_path:
        return True
    return field_path.split(".", 1)[0] in props


def _collect_refs(value: Any, top_field: str | None = None) -> list[tuple[str, OutputRef, bool]]:
    """Recursively collect ``$step`` / ``$step.field`` references from a step input.

    Returns ``(top_field, ref, is_pure)`` triples. ``top_field`` is the
    top-level input field name used for the ``ref_allowed`` (C-04) decision;
    nested references inside a field inherit that field's allowance.
    """
    refs: list[tuple[str, OutputRef, bool]] = []
    if isinstance(value, dict):
        for key, val in value.items():
            refs.extend(_collect_refs(val, key if top_field is None else top_field))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_refs(item, top_field))
    elif isinstance(value, str):
        pure = _REF_PURE_PATTERN.match(value)
        if pure and _is_plausible_step_id(pure.group(1)):
            refs.append(
                (
                    top_field or "",
                    OutputRef(step_id=pure.group(1), field_path=pure.group(2) or ""),
                    True,
                )
            )
        else:
            for m in _REF_INLINE_PATTERN.finditer(value):
                sid = m.group(1)
                if not _is_plausible_step_id(sid):
                    continue
                refs.append(
                    (top_field or "", OutputRef(step_id=sid, field_path=m.group(2) or ""), False)
                )
    return refs


def parse_output_refs(
    plan: DagPlan,
    *,
    registry: ToolRegistry,
    completed_step_ids: set[str] | None = None,
    available_step_ids: set[str] | None = None,
) -> list[str]:
    """S04: statically validate every ``$step`` reference in a plan.

    Returns a list of errors; empty list = valid.  Rules (D-01 / C-04):
      - referenced step exists in current plan ∪ completed ∪ available;
      - pure references: field exists in the source step's operation
        ``output_schema`` (shallow, lenient) AND the target input field is
        ``ref_allowed=True`` (unlisted fields default to False → reject);
      - inline references: step existence only (field validation lenient);
      - ``file_op.path`` / ``file_op.content`` never allow references.

    Invalid plans are rejected by the trusted PlanGuardrail before the Executor,
    so a ``$`` string can never reach a tool as a raw literal path.
    """
    errors: list[str] = []
    completed = set(completed_step_ids or ())
    available = set(available_step_ids or ())
    step_map = {s.id: s for s in plan.steps}
    valid_steps = set(step_map.keys()) | completed | available

    for step in plan.steps:
        if not isinstance(step.input, dict):
            continue
        target_op = None
        target_def = registry.get_tool_def(step.tool)
        if target_def is not None:
            target_op = target_def.resolve_operation(step.input)

        for top_field, ref, is_pure in _collect_refs(step.input):
            if ref.step_id not in valid_steps:
                errors.append(
                    f"Step '{step.id}': reference '${ref.step_id}' targets unknown step "
                    f"'{ref.step_id}' (not in plan, completed, or available)"
                )
                continue

            # C-04: target field must allow references (unlisted → False).
            if target_op is not None and not target_op.ref_allowed(top_field):
                errors.append(
                    f"Step '{step.id}': input field '{top_field}' does not allow "
                    f"$step.output references (ref_allowed=False)"
                )
                continue

            # Pure reference: source field must exist in the source step's
            # operation output_schema (only for in-plan source steps we know).
            if is_pure and ref.field_path and ref.step_id in step_map:
                src_step = step_map[ref.step_id]
                src_def = registry.get_tool_def(src_step.tool)
                if src_def is not None:
                    src_op = src_def.resolve_operation(src_step.input)
                    src_schema = src_op.output_schema if src_op is not None else src_def.output_schema
                    if not _schema_has_field(src_schema, ref.field_path):
                        op_label = f"'{src_op.operation}'" if src_op is not None else f"tool '{src_def.name}'"
                        errors.append(
                            f"Step '{step.id}': reference '${ref.step_id}.{ref.field_path}' — "
                            f"field '{ref.field_path}' not in operation {op_label} output_schema"
                        )

    return errors


def _step_is_mutating(step: DagStep, registry: ToolRegistry) -> bool:
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
            if not _step_is_mutating(step, registry):
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

            tool_def = self.registry.get_tool_def(step.tool)
            if tool_def is None:
                errors.append(f"Step '{step.id}': unknown tool '{step.tool}'")
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


class Planner:
    """Generates and revises DAG Plans via LLM.

    Non-trusted component. Output is validated by PlanGuardrail before execution.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        registry: ToolRegistry,
        store: EventStore | None = None,
        max_plan_retries: int = 2,
    ):
        self.llm = llm_client
        self.registry = registry
        self.store = store
        self.max_plan_retries = max_plan_retries
        self.guardrail = PlanGuardrail(registry, store)
        self.last_raw_response: str = ""

    async def _chat_structured(
        self,
        phase: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ):
        """S11 (问题八 观测侧): LLM 调用结构化日志 — run_id/phase/耗时/异常类型。"""
        _t0 = time.monotonic()
        try:
            resp = await self.llm.chat(messages, run_id=run_id, **kwargs)
            _log.info(
                "[llm] phase=%s run=%s duration_ms=%d chars=%d",
                phase,
                run_id or "",
                int((time.monotonic() - _t0) * 1000),
                len(resp.content) if resp and resp.content else 0,
            )
            return resp
        except Exception as exc:
            _log.error(
                "[llm] phase=%s run=%s duration_ms=%d error=%s:%s",
                phase,
                run_id or "",
                int((time.monotonic() - _t0) * 1000),
                type(exc).__name__,
                str(exc)[:200],
            )
            raise

    async def plan(
        self,
        intent: str,
        state: RunState | None = None,
        feedback: str | None = None,
        conversation_context: str = "",
        run_id: str | None = None,
    ) -> DagPlan | None:
        prompt = self._build_plan_prompt(
            intent,
            feedback=feedback,
            conversation_context=conversation_context,
        )
        _log.info(
            "[plan] phase=%s len=%d %s",
            AgentPhase.PLAN.value,
            len(prompt),
            fmtkv(intent=intent[:80], feedback_len=len(feedback) if feedback else 0, has_feedback=feedback is not None),
        )
        last_error = ""

        for attempt in range(1, self.max_plan_retries + 2):
            messages = [{"role": "system", "content": prompt}]
            if last_error:
                messages.append({"role": "user", "content": _retry_prompt(last_error)})

            _log.info("[plan] Attempt %d/%d for intent: %.80s", attempt, self.max_plan_retries + 1, intent)
            chat_resp = await self._chat_structured(AgentPhase.PLAN.value, run_id, messages, temperature=0.0)
            response = chat_resp.content
            _log.info(
                "[plan] LLM response (%d chars): %.200s%s",
                len(response),
                response,
                "..." if len(response) > 200 else "",
            )

            self.last_raw_response = response
            plan, last_error = self._parse_plan(response)
            if plan is None:
                _log.warning("[plan] Parse failed on attempt %d: %s", attempt, last_error)
                continue
            plan.user_intent = intent

            errors = self.guardrail.validate(plan)
            if errors:
                last_error = "; ".join(errors)
                _log.warning("[plan] Guardrail failed on attempt %d: %s", attempt, last_error)
                continue

            _log.info("[plan] Valid plan with %d steps", len(plan.steps))
            return plan

        _log.error("[plan] All %d attempts failed. Last error: %s", self.max_plan_retries + 1, last_error)
        return None

    async def revise(
        self,
        plan: DagPlan,
        results: dict[str, Any],
        system_state: str,
        feedback: str | None = None,
        intent_fallback: str = "",
        run_id: str | None = None,
    ) -> DagPlan | None:
        intent = plan.intent[:200] if plan.intent else (intent_fallback[:200] if intent_fallback else "(unknown)")
        user_intent = plan.user_intent[:200] if plan.user_intent else intent
        feedback_section = self._build_feedback_section(feedback)
        prompt = get_prompt(
            AgentPhase.REVISE,
            step_schema=_build_step_schema_text(),
            user_intent=user_intent,
            intent=intent,
            system_state=system_state,
            tool_descriptions=self._build_tool_descriptions(),
            feedback_section=feedback_section or "",
        )
        _log.info(
            "[revise] phase=%s len=%d %s\n=== REVISE SYSTEM STATE ===\n%s\n=== END REVISE SYSTEM STATE ===",
            AgentPhase.REVISE.value,
            len(prompt),
            fmtkv(intent=intent[:80], has_feedback=feedback is not None, feedback_len=len(feedback) if feedback else 0),
            system_state,
        )
        total_attempts = self.max_plan_retries + 1
        # Steps that must NOT be re-run (tool already executed with a settled
        # outcome). UNSUCCESSFUL steps are excluded — they may be re-run.
        executed_step_ids = {sid for sid, r in results.items() if isinstance(r, StepResult) and r.should_not_rerun}
        # Steps whose recorded output is available for $var.field references in
        # a revised plan — includes UNSUCCESSFUL (output_available), whose error
        # text is exactly what a summary step needs to report.
        available_step_ids = {sid for sid, r in results.items() if isinstance(r, StepResult) and r.output_available}

        last_error = ""
        for attempt in range(1, total_attempts + 1):
            messages = [{"role": "system", "content": prompt}]
            if last_error:
                messages.append({"role": "user", "content": _retry_prompt(last_error)})

            chat_resp = await self._chat_structured(AgentPhase.REVISE.value, run_id, messages, temperature=0.0)
            revised, last_error = self._parse_plan(chat_resp.content, executed_step_ids)

            if revised is None:
                _log.warning("[revise] Parse failed on attempt %d: %s", attempt, last_error)
                continue
            if not revised.user_intent:
                revised.user_intent = plan.user_intent

            if not revised.steps:
                _log.info("[revise] Attempt %d — task complete (empty steps)", attempt)
                return revised.model_copy(
                    update={
                        "steps": [],
                        "user_intent": plan.user_intent,
                        "declared_operations": list(plan.declared_operations),
                    }
                )

            errors = self.guardrail.validate(
                revised,
                completed_step_ids=executed_step_ids,
                available_step_ids=available_step_ids,
            )
            if errors:
                last_error = "; ".join(errors)
                _log.warning("[revise] Guardrail failed on attempt %d: %s", attempt, last_error)
                continue

            _log.info("[revise] Attempt %d — valid plan with %d steps", attempt, len(revised.steps))
            return revised

        _log.error("[revise] All %d attempts failed", total_attempts)
        return None

    async def generate_answer(
        self,
        intent: str,
        state: RunState,
        feedback: str | None,
        run_id: str | None = None,
        conversation_context: str = "",
    ) -> str:
        """Generate a conversational final answer when no tools are needed.

        All context (tool results, summary, feedback) is packed into a single
        user message so the LLM sees everything as content to answer, regardless
        of how different models handle multiple system messages.
        """
        prompt = get_prompt(AgentPhase.ANSWER)
        _log.info("[answer] phase=%s len=%d", AgentPhase.ANSWER.value, len(prompt))
        messages = [{"role": "system", "content": prompt}]

        parts = []
        n_tool_results = len(state.tool_results)

        # v2.2+ (JAGENT-2026-P1-13 Bug 4): 无工具执行时必须给 Answer 权威信号，
        # 防止模型自由发挥（如 325b42c5 对路径测试生成 CTF/Docker 泛化说明）。
        if n_tool_results == 0:
            parts.append(
                "[NO TOOLS EXECUTED] — no external tool was run for this task. "
                "Answer only from existing knowledge. Do not describe or imply any "
                "file operation, HTTP fetch, browser visit, or other external action."
            )

        if state.latest_plan:
            lp = state.latest_plan
            reason = lp.get("revision_reason")
            remaining = lp.get("remaining_steps_summary")
            pstatus = lp.get("status")
            if reason or remaining or pstatus:
                parts.append(
                    "[Run outcome — AUTHORITATIVE, from the event store. "
                    "The revision result below is exactly what the system decided; "
                    "do not describe the revision differently.]"
                )
                if reason:
                    parts.append(f"Last revision reason: {reason}")
                if remaining:
                    parts.append(f"Last revision result: {remaining}")
                if pstatus:
                    parts.append(f"Plan final status: {pstatus}")
                parts.append("")

        if state.tool_results:
            tool_inputs = {tc.tool_call_id: tc.input for tc in state.tool_calls}
            parts.append(
                "[Tool execution results — AUTHORITATIVE, exhaustive record "
                "of every tool call that actually ran in this task. "
                "Do NOT add or imply any execution not listed here.]"
            )
            parts.append("[Execution digest]")
            for i, tr in enumerate(state.tool_results):
                status_label = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
                parts.append(f"Step {i + 1}: {tr.tool_name} → {status_label}")
            parts.append("[Detailed results]")
            for i, tr in enumerate(state.tool_results):
                status_label = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
                parts.append(f"## Step {i + 1}: {tr.tool_name} (status: {status_label})")
                tc_input = tool_inputs.get(tr.tool_call_id)
                if tc_input:
                    input_str = str(tc_input)
                    if len(input_str) > 2000:
                        input_str = input_str[:2000] + "\n...(input truncated)..."
                    parts.append(f"Input: {input_str}")
                if tr.output is not None:
                    output_str = str(tr.output)
                    if len(output_str) > 5000:
                        output_str = output_str[:5000] + "\n...(truncated)..."
                    parts.append(f"Output: {output_str}")
                if tr.error:
                    parts.append(f"Error: {tr.error}")
                if tr.duration_ms:
                    parts.append(f"Duration: {tr.duration_ms}ms")
                parts.append("")

        if state.summary:
            if isinstance(state.summary, Episode):
                summary_parts = []
                if state.summary.title:
                    summary_parts.append(f"Title: {state.summary.title}")
                if state.summary.summary:
                    summary_parts.append(f"Summary: {state.summary.summary}")
                if state.summary.key_decisions:
                    summary_parts.append(f"Key decisions: {', '.join(state.summary.key_decisions)}")
                if state.summary.key_findings:
                    summary_parts.append(f"Key findings: {', '.join(state.summary.key_findings)}")
                if summary_parts:
                    parts.append("## Previous Context (Compressed)")
                    parts.extend(summary_parts)

        if state.feedbacks:
            fb_ids = ",".join(getattr(fb, "feedback_id", "?")[:8] for fb in state.feedbacks)
            _log.info("[answer] Including %d feedbacks %s", len(state.feedbacks), fmtkv(feedback_ids=fb_ids))
            parts.append("[Feedback]")
            for fb in state.feedbacks:
                parts.append(fb.feedback_text)
            parts.append("")

        user_content = "User's request:\n" + intent
        if conversation_context:
            user_content += "\n\nConversation context (reference only):\n" + conversation_context[:4000]
        if parts:
            user_content += "\n\n" + "\n".join(parts)

        messages.append({"role": "user", "content": user_content})
        total_chars = sum(len(m["content"]) for m in messages)
        _log.info(
            "[answer] Sending %d messages (%d tool_results, %d chars) to LLM",
            len(messages),
            n_tool_results,
            total_chars,
        )

        chat_resp = await self._chat_structured(
            AgentPhase.ANSWER.value,
            run_id,
            messages,
            temperature=0.7,
            max_tokens=16384,
        )
        _log.info(
            "[answer] LLM response: %d chars: %.200s%s",
            len(chat_resp.content),
            chat_resp.content,
            "..." if len(chat_resp.content) > 200 else "",
        )
        return chat_resp.content.strip()

    @staticmethod
    def _build_feedback_section(feedback: str | None) -> str:
        if not feedback:
            return ""
        _log.debug("[feedback] Built feedback section (%d chars)", len(feedback))
        return (
            f"\n## System Monitoring Feedback\n"
            f"{feedback}\n"
            f"Take this feedback into account when planning the next steps.\n"
        )

    def _build_plan_prompt(
        self,
        intent: str,
        feedback: str | None = None,
        conversation_context: str = "",
    ) -> str:
        text = get_prompt(
            AgentPhase.PLAN,
            step_schema=_build_step_schema_text(),
            tool_descriptions=self._build_tool_descriptions(),
            intent=intent,
        )
        fb = self._build_feedback_section(feedback)
        if conversation_context:
            # Conversation history is injected once for initial planning only.
            # It never contaminates the event-sourced current request or revise prompts.
            context = conversation_context[:4000]
            text = text.replace(
                "## User Intent\n",
                "## Conversation Context (reference only)\n" + context + "\n\n## User Intent\n",
            )
        if fb:
            text = text.replace("## User Intent\n", fb + "## User Intent\n")
        return text

    def _build_tool_descriptions(self) -> str:
        tool_defs = self.registry.list_tool_defs()
        lines = []
        for td in tool_defs:
            line = f"  - {td.name}: {td.description}"
            schema = td.input_schema
            if schema and isinstance(schema, dict):
                props = schema.get("properties", {})
                required = schema.get("required", [])
                if props:
                    param_lines = []
                    for pname, pinfo in props.items():
                        ptype = pinfo.get("type", "any")
                        req = "required" if pname in required else "optional"
                        parts = [f"      {pname} ({ptype}, {req})"]
                        enum = pinfo.get("enum")
                        if enum:
                            parts.append(f"allowed: {json.dumps(enum, ensure_ascii=False)}")
                        desc = pinfo.get("description", "")
                        if desc:
                            parts.append(f"— {desc}")
                        param_lines.append(" ".join(parts))
                    if param_lines:
                        line += "\n    Parameters:"
                        line += "\n" + "\n".join(param_lines)
            if td.requires_confirmation:
                line += " (requires confirmation)"
            lines.append(line)
        return "\n".join(lines) if lines else "  (no tools available)"

    @staticmethod
    def _parse_plan(response: str, executed_step_ids: set[str] | None = None) -> tuple[DagPlan | None, str]:
        """返回 (plan_or_None, error_reason)。error_reason 为空字符串表示成功。"""
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[-1]
            response = response.rsplit("```", 1)[0]
            response = response.strip()

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            start = response.find("{")
            end = response.rfind("}")
            if start != -1 and end > start:
                try:
                    data = json.loads(response[start : end + 1])
                except json.JSONDecodeError as e:
                    return None, f"JSON parse error: {e.msg} at position {e.pos}"
            else:
                return None, "No JSON object found in response"

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

            err = _validate_step(s, i)
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
