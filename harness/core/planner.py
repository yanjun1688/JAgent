"""Planner (V0.7, L4) — generates and revises DAG Plans via LLM.

Non-trusted component. Plan output is validated by PlanGuardrail before execution.
"""

from __future__ import annotations

import json
from typing import Any

from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger
from harness.models.events import EventType
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.registry import ToolRegistry

_log = agent_logger("planner")

_PLAN_PROMPT = """You are a task planner. Given a user intent and available tools,
create a step-by-step plan in JSON format. Each step calls one tool.

## Rules
1. Output ONLY valid JSON — no markdown, no code fences, no extra text.
2. Each step must have an id (unique, like "s1", "s2"), a tool name from the available tools, and an input dict.
3. If step B depends on step A's result, set B's "depends_on" to ["A_id"].
4. Independent steps (no depends_on) will be executed in parallel.
5. The plan is a DAG — no circular dependencies.
6. If the user's intent is a simple question/chat that needs no tools, return {{"steps": []}}.
   The content after "## User Intent" will be used as the direct answer.

## Output JSON format:
{{
  "steps": [
    {{"id": "s1", "tool": "tool_name", "input": {{"key": "value"}}}},
    {{"id": "s2", "tool": "tool_name", "input": {{"key": "value"}}, "depends_on": ["s1"]}}
  ]
}}

## Example 1 — Simple independent steps:
User: "Search for weather in Tokyo and London"
{{
  "steps": [
    {{"id": "s1", "tool": "browser_search", "input": {{"query": "Tokyo weather"}}}},
    {{"id": "s2", "tool": "browser_search", "input": {{"query": "London weather"}}}}
  ]
}}

## Example 2 — Dependent steps:
User: "Search for a recipe and save it to a file"
{{
  "steps": [
    {{"id": "s1", "tool": "browser_search", "input": {{"query": "chicken recipe"}}}},
    {{"id": "s2", "tool": "file_op", "input": {{"path": "recipe.txt", "content": "$s1_result"}}, "depends_on": ["s1"]}}
  ]
}}

## Available Tools
{tool_descriptions}

## User Intent
{intent}
"""

_REVISE_PROMPT = """You are a task planner reviewing execution results.
Some steps completed, some may have failed. Decide what to do next.

## Original User Intent
{intent}

{system_state}

## Output JSON format — same as before:
Return a revised plan with only the REMAINING steps (steps that haven't been executed yet).
If all steps are done, return {{"steps": []}}.
If the task cannot be completed, return {{"steps": [], "failed": true, "reason": "explanation"}}.

## Available Tools
{tool_descriptions}
"""

_RETRY_PROMPT = """The previous output was not valid JSON. Please output ONLY valid JSON for the plan.
No markdown, no code fences, no extra text — just the JSON object.
"""


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
            plan.topological_sort()
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

    async def plan(self, intent: str, state: RunState | None = None) -> DagPlan | None:
        prompt = self._build_plan_prompt(intent)
        last_error = ""

        for attempt in range(1, self.max_plan_retries + 2):
            messages = [{"role": "system", "content": prompt}]
            if last_error:
                messages.append({"role": "user", "content": f"Previous attempt failed: {last_error}. {_RETRY_PROMPT}"})

            _log.info("[plan] Attempt %d/%d for intent: %.60s", attempt, self.max_plan_retries + 1, intent)
            response = await self.llm.chat(messages, temperature=0.0)
            _log.info("[plan] LLM response (%d chars): %.200s", len(response), response)

            self.last_raw_response = response
            plan = self._parse_plan(response)
            if plan is None:
                last_error = "JSON parse failed — response was not valid JSON"
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
    ) -> DagPlan | None:
        prompt = _REVISE_PROMPT.format(
            intent=plan.intent[:200] if plan.intent else "(unknown)",
            system_state=system_state,
            tool_descriptions=self._build_tool_descriptions(),
        )
        total_attempts = self.max_plan_retries + 1
        completed_step_ids = {
            sid for sid, r in results.items()
            if isinstance(r, dict) and r.get("status") in ("completed", "idempotency_hit")
        }

        for attempt in range(1, total_attempts + 1):
            messages = [{"role": "system", "content": prompt}]
            if attempt > 1:
                messages.append({"role": "user", "content": _RETRY_PROMPT})

            response = await self.llm.chat(messages, temperature=0.0)
            plan = self._parse_plan(response)

            if plan is None:
                _log.warning("[revise] Parse failed on attempt %d", attempt)
                continue

            if not plan.steps:
                _log.info("[revise] Attempt %d — task complete (empty steps)", attempt)
                return DagPlan(intent=plan.intent, steps=[], dynamic=True)

            errors = self.guardrail.validate(plan, completed_step_ids=completed_step_ids)
            if errors:
                _log.warning("[revise] Guardrail failed on attempt %d: %s", attempt, "; ".join(errors))
                continue

            _log.info("[revise] Attempt %d — valid plan with %d steps", attempt, len(plan.steps))
            return plan

        _log.error("[revise] All %d attempts failed", total_attempts)
        return None

    async def generate_answer(self, intent: str, state: RunState, feedback: str | None) -> str:
        """Generate a conversational final answer when no tools are needed."""
        prompt = (
            "You are a helpful assistant. Answer the user's question directly and naturally.\n"
            "Do not call any tools. Just respond as a knowledgeable assistant.\n"
        )
        messages = [{"role": "system", "content": prompt}]
        if feedback:
            messages.append({"role": "system", "content": f"## Feedback\n{feedback}"})
        if state.summary:
            from harness.models.events import EpisodeSummary
            if isinstance(state.summary, EpisodeSummary):
                parts = []
                if state.summary.key_decisions:
                    parts.append(f"Key decisions: {', '.join(state.summary.key_decisions)}")
                if state.summary.key_findings:
                    parts.append(f"Key findings: {', '.join(state.summary.key_findings)}")
                if parts:
                    messages.append({"role": "system", "content": f"## Previous context\n" + "\n".join(parts)})
        messages.append({"role": "user", "content": intent})
        response = await self.llm.chat(messages, temperature=0.7, max_tokens=1024)
        return response.strip()

    def _build_plan_prompt(self, intent: str) -> str:
        return _PLAN_PROMPT.format(
            intent=intent,
            tool_descriptions=self._build_tool_descriptions(),
        )

    def _build_tool_descriptions(self) -> str:
        lines = []
        for td in self.registry.list_tool_defs():
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
    def _parse_plan(response: str) -> DagPlan | None:
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[-1]
            response = response.rsplit("```", 1)[0]
            response = response.strip()

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list):
            return None

        steps = []
        for s in steps_raw:
            if not isinstance(s, dict):
                return None

            step_input = s.get("input")
            if step_input is None:
                # Compatibility: LLM sometimes generates "parameters" instead of "input"
                step_input = s.get("parameters")
            if step_input is None or not isinstance(step_input, dict):
                return None

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
            dynamic=data.get("dynamic", False),
        )
