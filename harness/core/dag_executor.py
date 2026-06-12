"""DagExecutor — DAG-based parallel step execution (V0.7, L2 replacement for serial tool execution).

Given a DagPlan, executes steps in topological order,
running independent steps in parallel via asyncio.gather().
Each step goes through the full Tool Layer pipeline.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from harness.core.logger import agent_logger
from harness.tools.executor import ExecutionStatus
from harness.models.events import (
    DagStepCompletedPayload,
    DagStepFailedPayload,
    DagStepStartedPayload,
    EventType,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
)
from harness.models.plan import DagPlan
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry

_log = agent_logger("dag_executor")

_OUTPUT_SUMMARY_MAX_CHARS = 200


class PlanSuspended(Exception):
    """Raised when plan execution is suspended because steps need confirmation.
    
    The scheduler catches this, writes RUN_PAUSED, waits for operator,
    then retries each suspended step.
    """

    def __init__(self, confirmations: list[tuple[str, str]]):
        self.confirmations = confirmations  # [(step_id, confirmation_id), ...]
        ids = ", ".join(f"{sid}({cid})" for sid, cid in confirmations)
        super().__init__(f"{len(confirmations)} step(s) need confirmation: {ids}")


class DagExecutor:
    """Executes a DagPlan in topological layers, with parallelism within each layer."""

    def __init__(self, executor: ToolExecutor, store: EventStore, registry: ToolRegistry, max_parallel: int = 10):
        self.executor = executor
        self.store = store
        self.registry = registry
        self._semaphore = asyncio.Semaphore(max_parallel)

    async def execute(self, run_id: str, plan: DagPlan) -> dict[str, Any]:
        plan_id = f"plan_{run_id}_{int(time.time())}"
        layers = plan.topological_sort()

        _log.info("[plan] PlanCreated %s: %d steps in %d layers",
                  plan_id, len(plan.steps), len(layers))
        await self.store.append_event(
            run_id,
            EventType.PLAN_CREATED,
            PlanCreatedPayload(
                plan_id=plan_id,
                intent=plan.intent,
                steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
                layer_count=len(layers),
            ).model_dump(),
        )

        all_results: dict[str, Any] = {}
        total_completed = 0

        for layer_idx, layer in enumerate(layers):
            ok = await self._execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results)
            completed_here = sum(1 for sid in layer if all_results.get(sid, {}).get("status") == "completed")
            total_completed += completed_here
            if not ok:
                failed = [(sid, r.get("error", "unknown")) for sid, r in all_results.items() if r.get("status", "pending") != "completed"]
                if failed:
                    first_error = failed[0][1]
                    await self.store.append_event(
                        run_id,
                        EventType.PLAN_FAILED,
                        PlanFailedPayload(
                            plan_id=plan_id,
                            completed_steps=sum(1 for r in all_results.values() if isinstance(r, dict) and r.get("status") == "completed"),
                            total_layers=len(layers), final_error=first_error,
                        ).model_dump(),
                    )
                return all_results

        _log.info("[plan] PlanCompleted %s: %d/%d steps across %d layers",
                  plan_id, total_completed, len(plan.steps), len(layers))
        await self.store.append_event(
            run_id,
            EventType.PLAN_COMPLETED,
            PlanCompletedPayload(
                plan_id=plan_id, completed_steps=total_completed,
                total_layers=len(layers),
                summary=f"Completed {total_completed}/{len(plan.steps)} steps across {len(layers)} layers",
            ).model_dump(),
        )

        return all_results

    async def execute_layer(
        self, run_id: str, plan: DagPlan, plan_id: str,
        layer: list[str], layer_idx: int, layers: list[list[str]],
        all_results: dict[str, Any],
    ) -> bool:
        """Execute a single DAG layer.
        
        Returns:
            True if all steps completed.
            False if any step failed (plan terminated).
        
        Raises:
            PlanSuspended: if a step needs human confirmation.
        """
        return await self._execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results)

    async def retry_step(
        self, run_id: str, plan: DagPlan, step_id: str,
        all_results: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-execute a single DAG step after confirmation resume.
        
        Unlike _execute_step_only, this does NOT write DAG_STEP_STARTED
        (already written during the original layer execution).
        Returns the same dict format as _execute_step_only.
        """
        step = next((s for s in plan.steps if s.id == step_id), None)
        if step is None:
            return {"status": "error", "error": f"Step '{step_id}' not found in plan"}

        upstream = plan.upstream_outputs(step_id, all_results)

        _referenced_vars = set()
        for value in step.input.values():
            if isinstance(value, str):
                _referenced_vars.update(
                    m.group(1) for m in re.finditer(r'\$(\w+)', value)
                )
        for var_name in _referenced_vars:
            if var_name not in upstream and var_name in all_results:
                result = all_results[var_name]
                if isinstance(result, dict) and result.get("status") == "completed":
                    upstream[var_name] = result.get("output")
                    _log.info("[var] $%s resolved from historical results (retry, not in depends_on)", var_name)

        merged_input = self._resolve_variables_in_input(step.input, upstream)

        step_def = self.registry.get_tool_def(step.tool)
        step_fn = self.registry.get_tool_fn(step.tool)

        if step_def is None or step_fn is None:
            return {"status": "error", "error": f"Tool '{step.tool}' not registered"}

        _log.info("[retry] %s → %s after confirmation resume", step_id, step.tool)

        async with self._semaphore:
            result = await self.executor.execute(
                run_id, step.tool, merged_input, step_def, step_fn,
            )

        if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
            summary = self._truncate_output(result.output, _OUTPUT_SUMMARY_MAX_CHARS)
            _log.info("[retry] %s → %s completed (%.0fms)", step_id, step.tool, getattr(result, 'duration_ms', 0))
            return {"status": "completed", "output": result.output, "summary": summary}

        if result.status == ExecutionStatus.CONFIRMATION_NEEDED:
            _log.info("[retry] %s → %s still needs confirmation (id=%s)", step_id, step.tool, result.confirmation_id)
            return {
                "status": "confirmation_needed",
                "confirmation_id": result.confirmation_id,
                "step_id": step_id,
            }

        error = result.error or f"Step failed with status {result.status.value}"
        _log.warning("[retry] %s → %s failed: %s", step_id, step.tool, error[:200])
        return {"status": "error", "error": error, "retryable": getattr(result, "retryable", False)}

    async def _execute_layer(
        self, run_id: str, plan: DagPlan, plan_id: str,
        layer: list[str], layer_idx: int, layers: list[list[str]],
        all_results: dict[str, Any],
    ) -> bool:
        _log.info("[layer %d/%d] %d step(s): %s",
                  layer_idx + 1, len(layers), len(layer), ", ".join(layer))

        # Step 1: Write all DAG_STEP_STARTED events serially (seq-guaranteed)
        for sid in layer:
            step = next((s for s in plan.steps if s.id == sid), None)
            if step:
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_STARTED,
                    DagStepStartedPayload(
                        plan_id=plan_id, step_id=sid,
                        tool_name=step.tool, depends_on=step.depends_on,
                    ).model_dump(),
                )

        # Step 2: Execute all steps concurrently (pure execution, no event writing)
        tasks = [
            self._execute_step_only(run_id, plan, all_results, sid)
            for sid in layer
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Step 3: Write DAG_STEP_COMPLETED/FAILED events serially (seq-guaranteed)
        any_failed = False
        pending_confirmations: list[tuple[str, str]] = []
        for sid, raw in zip(layer, raw_results):
            if isinstance(raw, Exception):
                error = f"DagExecutor layer {layer_idx}: {raw}"
                _log.error("[fail] step=%s error=%s", sid, error)
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id, step_id=sid, error=error,
                    ).model_dump(),
                )
                all_results[sid] = {"error": error, "status": "executor_error"}
                any_failed = True
                continue

            if raw.get("status") == "completed":
                all_results[sid] = raw
                _log.info("[step] %s completed — %s", sid, str(raw.get("summary", "")))
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_COMPLETED,
                    DagStepCompletedPayload(
                        plan_id=plan_id, step_id=sid,
                        output_summary=raw.get("summary", ""),
                    ).model_dump(),
                )
            elif raw.get("status") == "confirmation_needed":
                all_results[sid] = raw
                cid = raw.get("confirmation_id")
                pending_confirmations.append((sid, cid))
                _log.info("[step] %s needs confirmation (id=%s)", sid, cid)
            else:
                err = raw.get("error", "unknown")
                _log.error("[fail] step=%s error=%s", sid, err)
                all_results[sid] = raw
                any_failed = True
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id, step_id=sid, error=err,
                        retryable=raw.get("retryable", False),
                    ).model_dump(),
                )

        if pending_confirmations:
            raise PlanSuspended(confirmations=pending_confirmations)

        if any_failed:
            _log.warning("[layer %d/%d] %d/%d step(s) failed",
                         layer_idx + 1, len(layers),
                         sum(1 for r in raw_results if isinstance(r, dict) and r.get("status") != "completed"),
                         len(layer))
            return False

        _log.info("[layer %d/%d] all %d step(s) completed", layer_idx + 1, len(layers), len(layer))
        return True

    async def _execute_step_only(
        self, run_id: str, plan: DagPlan,
        all_results: dict[str, Any], step_id: str,
    ) -> dict[str, Any]:
        """Execute a single DAG step without writing events.
        
        Returns dict with status/output/error/summary.
        Event writing is handled by _execute_layer.
        """
        step = next((s for s in plan.steps if s.id == step_id), None)
        if step is None:
            return {"status": "error", "error": f"Step '{step_id}' not found in plan"}

        upstream = plan.upstream_outputs(step_id, all_results)
        for dep_id in step.depends_on:
            legacy_key = f"{dep_id}_result"
            if legacy_key not in upstream and dep_id in upstream:
                upstream[legacy_key] = upstream[dep_id]

        _referenced_vars = set()
        for value in step.input.values():
            if isinstance(value, str):
                _referenced_vars.update(
                    m.group(1) for m in re.finditer(r'\$(\w+)', value)
                )
        for var_name in _referenced_vars:
            if var_name not in upstream and var_name in all_results:
                result = all_results[var_name]
                if isinstance(result, dict) and result.get("status") == "completed":
                    upstream[var_name] = result.get("output")
                    _log.info("[var] $%s resolved from historical results (not in depends_on)", var_name)

        merged_input = self._resolve_variables_in_input(step.input, upstream)

        step_def = self.registry.get_tool_def(step.tool)
        step_fn = self.registry.get_tool_fn(step.tool)

        if step_def is None or step_fn is None:
            return {"status": "error", "error": f"Tool '{step.tool}' not registered"}

        _log.info("[step] %s → %s with %d param(s)", step_id, step.tool, len(merged_input))

        async with self._semaphore:
            result = await self.executor.execute(
                run_id, step.tool, merged_input, step_def, step_fn,
            )

        if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
            summary = self._truncate_output(result.output, _OUTPUT_SUMMARY_MAX_CHARS)
            _log.info("[step] %s → %s completed (%.0fms)", step_id, step.tool, getattr(result, 'duration_ms', 0))
            return {"status": "completed", "output": result.output, "summary": summary}

        if result.status == ExecutionStatus.CONFIRMATION_NEEDED:
            _log.info("[step] %s → %s needs confirmation (id=%s)", step_id, step.tool, result.confirmation_id)
            return {
                "status": "confirmation_needed",
                "confirmation_id": result.confirmation_id,
                "step_id": step_id,
            }

        error = result.error or f"Step failed with status {result.status.value}"
        _log.warning("[step] %s → %s failed: %s", step_id, step.tool, error[:200])
        return {"status": "error", "error": error, "retryable": getattr(result, "retryable", False)}

    @staticmethod
    def _resolve_variables_in_input(step_input: dict, upstream: dict[str, Any]) -> dict:
        resolved = {}
        for key, value in step_input.items():
            if isinstance(value, str):
                pure = re.match(r'^\$(\w+)(?:\.([\w.]+))?$', value)
                if pure:
                    var_name = pure.group(1)
                    path = pure.group(2)
                    uv = upstream.get(var_name)
                    if uv is not None:
                        if path:
                            for part in path.split("."):
                                if isinstance(uv, dict):
                                    uv = uv.get(part)
                                else:
                                    uv = None
                                    break
                            if uv is None and isinstance(upstream.get(var_name), dict):
                                uv = DagExecutor._deep_resolve(upstream[var_name], path.split("."))
                                if uv is not None:
                                    _log.info("[var] $%s.%s resolved via deep search", var_name, path)
                        if uv is not None:
                            resolved[key] = uv
                            continue
                resolved[key] = DagExecutor._substitute_vars(value, upstream)
            elif isinstance(value, dict):
                resolved[key] = DagExecutor._resolve_variables_in_input(value, upstream)
            elif isinstance(value, list):
                resolved_list = []
                for item in value:
                    if isinstance(item, str):
                        resolved_list.append(DagExecutor._substitute_vars(item, upstream))
                    elif isinstance(item, dict):
                        resolved_list.append(DagExecutor._resolve_variables_in_input(item, upstream))
                    else:
                        resolved_list.append(item)
                resolved[key] = resolved_list
            else:
                resolved[key] = value
        return resolved

    @staticmethod
    def _substitute_vars(text: str, upstream: dict[str, Any]) -> str:
        def _replacer(m: re.Match) -> str:
            var_name = m.group(1)
            path = m.group(2)
            value = upstream.get(var_name)
            if value is None:
                _log.warning("[var] '%s' not found in upstream outputs (key exists: %s)",
                             var_name, var_name in upstream)
                return m.group(0)
            if path:
                parts = path.split(".")
                for part in parts:
                    if isinstance(value, dict):
                        value = value.get(part)
                    elif isinstance(value, str):
                        try:
                            parsed = json.loads(value)
                        except (json.JSONDecodeError, TypeError):
                            _log.warning("[var] path '%s' blocked at '%s' — value is non-JSON string",
                                         path, part)
                            return m.group(0)
                        if isinstance(parsed, dict):
                            value = parsed.get(part)
                        else:
                            _log.warning("[var] path '%s' blocked at '%s' — parsed JSON is not dict",
                                         path, part)
                            return m.group(0)
                    else:
                        _log.warning("[var] path '%s' not found in variable '%s' (stopped at '%s')",
                                     path, var_name, part)
                        return m.group(0)
                if value is None and isinstance(upstream.get(var_name), dict):
                    found = DagExecutor._deep_resolve(upstream[var_name], parts)
                    if found is not None:
                        value = found
                        _log.info("[var] $%s.%s resolved via deep search", var_name, path)
            return str(value)
        return re.sub(r'\$(\w+)(?:\.([\w.]+))?', _replacer, text)

    @staticmethod
    def _deep_resolve(output: dict, parts: list[str], _depth: int = 0) -> Any:
        if _depth > 5 or not output:
            return None
        for v in output.values():
            if isinstance(v, dict):
                current = v
                for part in parts:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        break
                else:
                    return current
                result = DagExecutor._deep_resolve(v, parts, _depth + 1)
                if result is not None:
                    return result
            elif isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, dict):
                        current = parsed
                        for part in parts:
                            if isinstance(current, dict) and part in current:
                                current = current[part]
                            else:
                                break
                        else:
                            return current
                        result = DagExecutor._deep_resolve(parsed, parts, _depth + 1)
                        if result is not None:
                            return result
                except (json.JSONDecodeError, TypeError):
                    pass
        return None

    @staticmethod
    def _truncate_output(output: Any, max_chars: int = _OUTPUT_SUMMARY_MAX_CHARS) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output[:max_chars]
        text = json.dumps(output, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            return text[:max_chars] + "..."
        return text

    @staticmethod
    def build_dag_status_text(plan: DagPlan, results: dict[str, Any], current_layer: int) -> str:
        """Build a structured status summary for Planner.revise() injection.

        Marked with 【系统状态 - 不可折叠】 so Context Manager skips compression.
        Includes original step input for every step so the LLM can regenerate
        correct parameters on revise.
        """
        lines = ["【系统状态 - 不可折叠】"]
        lines.append(f"Plan: {plan.intent[:60]}")
        layers = []
        try:
            layers = plan.topological_sort()
        except ValueError:
            layers = []
        lines.append(f"Total layers: {len(layers)} | Current layer: {current_layer + 1}/{len(layers)}")
        lines.append(f"Total steps: {len(plan.steps)} | Completed: {sum(1 for r in results.values() if r.get('status') == 'completed')}")
        lines.append("")

        input_trunc = 200
        for step in plan.steps:
            r = results.get(step.id)
            status_tag = ""
            detail = ""
            input_str = json.dumps(step.input, ensure_ascii=False)[:input_trunc]

            if r is None:
                status_tag = "[pending]"
                detail = f"Input: {input_str}"
            elif r.get("status") == "completed":
                status_tag = "[done]"
                summary = r.get("summary", "")
                detail = f"Summary: {summary[:80]}" if summary else "OK"
            elif r.get("status") == "skipped":
                status_tag = "[skipped]"
                detail = f"Input: {input_str} | {r.get('reason', 'skipped')}"
            else:
                status_tag = "[failed]"
                detail = f"Input: {input_str} | Error: {r.get('error', 'unknown')}"

            deps = f" | Depends: {','.join(step.depends_on)}" if step.depends_on else ""
            lines.append(f"  - {step.id}({step.tool}): {status_tag}{deps}")
            lines.append(f"    {detail}")

        return "\n".join(lines)
