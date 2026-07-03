"""Planner (V0.7, L4) — generates and revises DAG Plans via LLM.

Non-trusted component. Plan output is validated by PlanGuardrail before execution.
"""

from __future__ import annotations

import json
from typing import Any

from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger, fmtkv
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import EpisodeSummary, EventType
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import ToolDefinition
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
            "type": "array", "items": {"type": "string"},
            "description": "IDs of steps this step depends on (empty if independent)",
        },
        "description": {"type": "string", "description": "What this step does"},
    },
    "required": ["id", "tool", "input"],
    "additionalProperties": False,
}


def _build_step_schema_text() -> str:
    """Return LLM-readable schema text with single braces (for direct use)."""
    return """Top-level JSON MUST contain:
  - "intent" (string, required): a one-sentence summary of what this plan aims to accomplish. Rephrase the user's goal in your own words — DO NOT copy-paste the user intent verbatim.
  - "steps" (array): list of step objects. Use [] for no-action plans.

Each step MUST be a JSON object with exactly these fields:
  - "id" (string, required): unique identifier, e.g. "s1"
  - "tool" (string, required): tool name from the available tools list
  - "input" (object, required): ALL parameters go inside this object.
    NEVER put parameters like 'action', 'url', 'query' at the step level.
    Good: {"id": "s1", "tool": "http_request", "input": {"action": "GET", "url": "..."}}
    Bad:  {"id": "s1", "tool": "http_request", "action": "GET", "url": "..."}
  - "depends_on" (array of strings, optional): step dependencies for DAG ordering
  - "description" (string, optional): what this step does

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


class PlanGuardrail:
    """Validates a DagPlan before execution — tool existence, schema, cycle, safety."""

    def __init__(self, registry: ToolRegistry, store: EventStore | None = None):
        self.registry = registry
        self.store = store

    def validate(self, plan: DagPlan, completed_step_ids: set[str] | None = None) -> list[str]:
        errors = []
        completed = completed_step_ids or set()

        if not plan.steps:
            return []

        step_ids = {s.id for s in plan.steps}

        for i, step in enumerate(plan.steps):
            if not step.id:
                errors.append(f"Step {i} is missing 'id' field")
                continue
            if step.id in [s.id for s in plan.steps[:i]]:
                errors.append(f"Duplicate step id '{step.id}'")
                continue

            tool_def = self.registry.get_tool_def(step.tool)
            if tool_def is None:
                errors.append(f"Step '{step.id}': unknown tool '{step.tool}'")
                continue

            if not isinstance(step.input, dict):
                errors.append(f"Step '{step.id}': 'input' must be an object")
                continue

            for dep in step.depends_on:
                if dep not in step_ids and dep not in completed:
                    errors.append(f"Step '{step.id}': depends on unknown step '{dep}'")

        if errors:
            return errors

        try:
            plan.topological_sort(completed_step_ids=completed)
        except ValueError as e:
            errors.append(str(e))

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
                            f"Dangerous combination: '{step.tool}' and '{dangerous}' "
                            f"cannot appear in the same plan"
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
                        step.tool, count_in_layer, limit,
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

    async def plan(
        self, intent: str,
        state: RunState | None = None,
        feedback: str | None = None,
    ) -> DagPlan | None:
        prompt = self._build_plan_prompt(intent, feedback=feedback)
        _log.info("[plan] phase=%s len=%d %s",
                  AgentPhase.PLAN.value, len(prompt),
                  fmtkv(intent=intent[:80], feedback_len=len(feedback) if feedback else 0,
                        has_feedback=feedback is not None))
        last_error = ""

        for attempt in range(1, self.max_plan_retries + 2):
            messages = [{"role": "system", "content": prompt}]
            if last_error:
                messages.append({"role": "user", "content": _retry_prompt(last_error)})

            _log.info("[plan] Attempt %d/%d for intent: %s", attempt, self.max_plan_retries + 1, intent)
            response = await self.llm.chat(messages, temperature=0.0)
            _log.info("[plan] LLM response (%d chars): %s", len(response), response)

            self.last_raw_response = response
            plan, last_error = self._parse_plan(response)
            if plan is None:
                _log.warning("[plan] Parse failed on attempt %d: %s", attempt, last_error)
                continue

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
    ) -> DagPlan | None:
        intent = plan.intent[:200] if plan.intent else (intent_fallback[:200] if intent_fallback else "(unknown)")
        feedback_section = self._build_feedback_section(feedback)
        prompt = get_prompt(
            AgentPhase.REVISE,
            step_schema=_build_step_schema_text(),
            intent=intent,
            system_state=system_state,
            tool_descriptions=self._build_tool_descriptions(),
            feedback_section=feedback_section or "",
        )
        _log.info("[revise] phase=%s len=%d %s\n=== REVISE SYSTEM STATE ===\n%s\n=== END REVISE SYSTEM STATE ===",
                  AgentPhase.REVISE.value, len(prompt),
                  fmtkv(intent=intent[:80], has_feedback=feedback is not None,
                        feedback_len=len(feedback) if feedback else 0),
                  system_state)
        total_attempts = self.max_plan_retries + 1
        completed_step_ids = {
            sid for sid, r in results.items()
            if isinstance(r, dict) and r.get("status") in ("completed", "idempotency_hit")
        }

        last_error = ""
        for attempt in range(1, total_attempts + 1):
            messages = [{"role": "system", "content": prompt}]
            if last_error:
                messages.append({"role": "user", "content": _retry_prompt(last_error)})

            response = await self.llm.chat(messages, temperature=0.0)
            revised, last_error = self._parse_plan(response)

            if revised is None:
                _log.warning("[revise] Parse failed on attempt %d: %s", attempt, last_error)
                continue

            if not revised.steps:
                _log.info("[revise] Attempt %d — task complete (empty steps)", attempt)
                return DagPlan(intent=revised.intent, steps=[])

            errors = self.guardrail.validate(revised, completed_step_ids=completed_step_ids)
            if errors:
                last_error = "; ".join(errors)
                _log.warning("[revise] Guardrail failed on attempt %d: %s", attempt, last_error)
                continue

            _log.info("[revise] Attempt %d — valid plan with %d steps", attempt, len(revised.steps))
            return revised

        _log.error("[revise] All %d attempts failed", total_attempts)
        return None

    async def generate_answer(self, intent: str, state: RunState, feedback: str | None) -> str:
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

        if state.tool_results:
            tool_inputs = {
                tc.tool_call_id: tc.input
                for tc in state.tool_calls
            }
            parts.append("[Tool execution results]")
            for i, tr in enumerate(state.tool_results):
                status_label = tr.status.value if hasattr(tr.status, 'value') else str(tr.status)
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
            if isinstance(state.summary, EpisodeSummary):
                summary_parts = []
                if state.summary.key_decisions:
                    summary_parts.append(f"Key decisions: {', '.join(state.summary.key_decisions)}")
                if state.summary.key_findings:
                    summary_parts.append(f"Key findings: {', '.join(state.summary.key_findings)}")
                if summary_parts:
                    parts.append("## Previous Context (Compressed)")
                    parts.extend(summary_parts)

        if state.feedbacks:
            fb_ids = ",".join(getattr(fb, "feedback_id", "?")[:8] for fb in state.feedbacks)
            _log.info("[answer] Including %d feedbacks %s", len(state.feedbacks),
                      fmtkv(feedback_ids=fb_ids))
            parts.append("[Feedback]")
            for fb in state.feedbacks:
                parts.append(fb.feedback_text)
            parts.append("")

        user_content = "User's request:\n" + intent
        if parts:
            user_content += "\n\n" + "\n".join(parts)

        messages.append({"role": "user", "content": user_content})
        total_chars = sum(len(m["content"]) for m in messages)
        _log.info("[answer] Sending %d messages (%d tool_results, %d chars) to LLM",
                  len(messages), n_tool_results, total_chars)
        _log.info("[answer] === USER MESSAGE BEGIN ===\n%s\n=== USER MESSAGE END ===", user_content)

        response = await self.llm.chat(messages, temperature=0.7, max_tokens=16384)
        _log.info("[answer] LLM response: %d chars: %s", len(response), response)
        return response.strip()

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

    def _build_plan_prompt(self, intent: str, feedback: str | None = None) -> str:
        text = get_prompt(
            AgentPhase.PLAN,
            step_schema=_build_step_schema_text(),
            tool_descriptions=self._build_tool_descriptions(intent=intent),
            intent=intent,
        )
        fb = self._build_feedback_section(feedback)
        if fb:
            text = text.replace("## User Intent\n", fb + "## User Intent\n")
        return text

    @staticmethod
    def _extract_tool_keywords(td: "ToolDefinition") -> set[str]:
        """Extract relevance keywords from a tool definition."""
        kw = set()
        kw.add(td.name.lower())
        for word in td.description.lower().split():
            clean = word.strip(".,;:()[]")
            if len(clean) >= 3:
                kw.add(clean)
        if td.input_schema and isinstance(td.input_schema, dict):
            for pname in td.input_schema.get("properties", {}):
                kw.add(pname.lower())
            for pname, pinfo in td.input_schema.get("properties", {}).items():
                desc = pinfo.get("description", "")
                if desc:
                    for word in desc.lower().split():
                        clean = word.strip(".,;:()[]")
                        if len(clean) >= 3:
                            kw.add(clean)
        return kw

    @staticmethod
    def _filter_tools_by_intent(intent: str, tool_defs: list["ToolDefinition"]) -> list["ToolDefinition"]:
        """Return tools whose keywords appear in the intent, plus always-include tools."""
        intent_lower = intent.lower()
        ALWAYS_INCLUDE = {"file_op"}
        relevant = []
        for td in tool_defs:
            if td.name in ALWAYS_INCLUDE:
                relevant.append(td)
                continue
            keywords = Planner._extract_tool_keywords(td)
            if any(kw in intent_lower for kw in keywords):
                relevant.append(td)
        return relevant if relevant else tool_defs

    def _build_tool_descriptions(self, intent: str | None = None) -> str:
        tool_defs = self.registry.list_tool_defs()
        if intent and len(tool_defs) > 2:
            tool_defs = self._filter_tools_by_intent(intent, tool_defs)
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
    def _parse_plan(response: str) -> tuple[DagPlan | None, str]:
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
                    data = json.loads(response[start:end+1])
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

            steps.append(DagStep(
                id=s.get("id", ""),
                tool=s.get("tool", ""),
                input=step_input,
                depends_on=s.get("depends_on", []),
                description=s.get("description", ""),
            ))

        return DagPlan(
            intent=data.get("intent", ""),
            steps=steps,
        ), ""
