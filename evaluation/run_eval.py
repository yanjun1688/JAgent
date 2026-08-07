"""JAgent offline evaluation entrypoint (Phase 4).

Runs each eval case through the real Agent pipeline, computes rule-based and
(optionally) LLM-as-Judge scores, and attaches them to the corresponding
Langfuse trace so they are visible in the Langfuse dashboard.

Modes:
  * In-process (default): assembles the same trusted components as serve.py and
    runs the scheduler directly. No server required.
  * HTTP mode (--api-base): POSTs runs to a running JAgent server and polls
    until the run reaches a terminal state.

Usage:
    uv run python evaluation/run_eval.py --dataset evaluation/datasets/jagent_eval.yaml
    uv run python evaluation/run_eval.py --dataset evaluation/datasets/jagent_eval.yaml --scenario "单步工具调用"
    uv run python evaluation/run_eval.py --dataset evaluation/datasets/jagent_eval.yaml --case-id multi_step_001
    uv run python evaluation/run_eval.py --dataset evaluation/datasets/jagent_eval.yaml --consistency-runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evaluation.datasets.base import DatasetLoader, EvalCase  # noqa: E402
from evaluation.scorers.llm_judge import LLMJudgeScorer  # noqa: E402
from evaluation.scorers.rule_based import compute_rule_scores  # noqa: E402
from harness.models.events import (  # noqa: E402
    ConfirmationReceivedPayload,
    EventType,
)
from harness.models.tools import (  # noqa: E402
    Guardrail,
    RetryPolicy,
    SideEffect,
    ToolDefinition,
)

# ── Eval-only tool: exec (requires confirmation) ────────────────────────
# Not part of the production registry — registered only inside the eval
# engine so the 确认流程 scenario can be exercised end-to-end.

_EXEC_CONFIRM_DEF = ToolDefinition(
    name="exec",
    description="Execute an arbitrary shell command (requires operator confirmation).",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
        },
        "required": ["command"],
    },
    output_schema={
        "type": "object",
        "properties": {"stdout": {"type": "string"}},
    },
    idempotency_key_fields=["command"],
    side_effects=[SideEffect.EXTERNAL],
    requires_confirmation=True,
    timeout_ms=30000,
    retry_policy=RetryPolicy(max_retries=0),
)


async def _exec_confirm_fn(input: dict[str, Any]) -> dict[str, Any]:
    return {"stdout": f"(eval) simulated exec: {input.get('command', '')[:100]}"}


# ── Eval-only tool: http_request with rate_limit guardrail ──────────────
# Production http_request has only a scope guardrail (no rate_limit), so the
# rate-limit scenario uses this eval-only definition to exercise the trusted
# RateLimitGuardrail deterministically.

_HTTP_RATE_LIMIT_DEF = ToolDefinition(
    name="http_request_rl",
    description="Send an HTTP request (eval-only, rate limited to 3 calls).",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {"status": {"type": "integer"}},
    },
    idempotency_key_fields=["url", "method"],
    side_effects=[SideEffect.EXTERNAL],
    guardrails=[Guardrail(guardrail_type="rate_limit", config={"max_calls": 3})],
    timeout_ms=30000,
    retry_policy=RetryPolicy(max_retries=0),
)


async def _http_rate_limit_fn(input: dict[str, Any]) -> dict[str, Any]:
    return {"status": 200}


def _expand_placeholders(value: Any) -> Any:
    """Recursively replace ``@project@`` with the repo root.

    Used by both mock_actions and mock_plan so scenarios can reference absolute
    project paths without hard-coding them.
    """
    if isinstance(value, str):
        return value.replace("@project@", str(ROOT))
    if isinstance(value, dict):
        return {k: _expand_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_placeholders(v) for v in value]
    return value
from harness.core.dag_executor import DagExecutor  # noqa: E402
from harness.core.fold import fold_events  # noqa: E402
from harness.core.llm_client import OpenAILLMClient  # noqa: E402
from harness.core.planner import Planner  # noqa: E402
from harness.core.scheduler import PlanningExecutorScheduler, SchedulerConfig  # noqa: E402
from harness.core.scheduler.base import ThinkResult  # noqa: E402
from harness.core.scheduler.loop import AgentLoopScheduler  # noqa: E402
from harness.monitoring.langfuse_tracer import LangfuseTracer  # noqa: E402
from harness.storage.event_store import EventStore  # noqa: E402
from harness.tools.browser_tool import BROWSER_DEF, browser_fn  # noqa: E402
from harness.tools.executor import ToolExecutor  # noqa: E402
from harness.tools.file_op import FILE_OP_DEF, file_op_fn, set_sandbox_root  # noqa: E402
from harness.tools.http_request import HTTP_REQUEST_DEF, http_request_fn  # noqa: E402
from harness.tools.mcp_call import MCP_CALL_DEF, mcp_call_fn  # noqa: E402
from harness.tools.registry import ToolRegistry  # noqa: E402

# ── CLI ────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run JAgent offline evaluation cases")
    p.add_argument("--dataset", default=str(ROOT / "evaluation" / "datasets" / "jagent_eval.yaml"),
                   help="Path to eval dataset YAML")
    p.add_argument("--scenario", default=None, help="Only run cases in this scenario")
    p.add_argument("--case-id", default=None, help="Only run this case id")
    p.add_argument("--api-base", default=None, help="HTTP mode: base URL of a running JAgent server")
    p.add_argument("--timeout", type=float, default=300.0, help="Per-case timeout in seconds")
    p.add_argument("--max-iterations", type=int, default=20, help="Scheduler max_iterations")
    p.add_argument("--no-llm-judge", action="store_true", help="Skip LLM-as-Judge scoring")
    p.add_argument("--consistency-runs", type=int, default=0,
                   help="Run each case N times to compute tool-sequence consistency (0=disabled)")
    p.add_argument("--upload-to-langfuse", action="store_true",
                   help="Also upload the dataset to Langfuse (requires LANGFUSE keys)")
    return p


# ── In-process engine assembly ─────────────────────────────────────────


class _InProcessEngine:
    """Assemble and run the same trusted pipeline as serve.py."""

    def __init__(self, max_iterations: int, tracer: LangfuseTracer | None = None) -> None:
        self.store = EventStore(":memory:")
        self.executor = ToolExecutor(self.store)
        self.registry = ToolRegistry()
        for td, fn in (
            (HTTP_REQUEST_DEF, http_request_fn),
            (FILE_OP_DEF, file_op_fn),
            (BROWSER_DEF, browser_fn),
            (MCP_CALL_DEF, mcp_call_fn),
        ):
            self.registry.register(td, fn)
        self.registry.register(_EXEC_CONFIRM_DEF, _exec_confirm_fn)
        self.registry.register(_HTTP_RATE_LIMIT_DEF, _http_rate_limit_fn)
        self.max_iterations = max_iterations
        self.tracer = tracer
        self.llm_client: OpenAILLMClient | None = None
        if os.environ.get("LLM_API_KEY"):
            self.llm_client = OpenAILLMClient(
                api_key=os.environ["LLM_API_KEY"],
                model=os.environ.get("LLM_MODEL_NAME", "qwen3.7-max-preview"),
                base_url=os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            )

    async def run_case(self, case: EvalCase) -> dict[str, Any]:
        await self.store.initialize()
        run_id = f"eval_{case.id}"
        config = SchedulerConfig(max_iterations=self.max_iterations)
        if case.mock_actions:
            # Trusted-component scenarios (Guardrail / confirmation): drive
            # deterministically with a MockAgentKernel that issues the scripted
            # tool calls, so the trusted guardrails are exercised without
            # relying on an LLM's willingness to attempt dangerous operations.
            from harness.tools.guardrails import RateLimitGuardrail

            RateLimitGuardrail.reset()
            # Isolate mock file operations in a temp sandbox so a scope-blocked
            # target is outside it (→ blocked) while a confirmation target stays
            # inside it (→ executes harmlessly in temp).
            tmp_root = tempfile.mkdtemp(prefix="jagent_eval_sandbox_")
            set_sandbox_root(tmp_root)
            try:
                kernel = self._build_mock_kernel(case)
                scheduler: Any = AgentLoopScheduler(
                    self.store, self.executor, kernel,
                    self.registry.list_tool_defs(), self.registry.list_tool_fns(),
                    config=config, tracer=self.tracer,
                )
                state = await self._run_with_autoconfirm(scheduler, run_id, case)
            finally:
                set_sandbox_root(str(ROOT))
            events = await self.store.get_events(run_id)
            return {"run_id": run_id, "state": state, "events": events}
        if case.mock_plan:
            # DAG topology scenarios: drive deterministically with a
            # MockLLMClient that returns the pre-built plan verbatim, so DAG
            # layer/parallelism/event-order behaviour is verified without LLM
            # variance. file_op steps read real project files (read-only), so
            # the sandbox root is the repo root for the duration of the run.
            from harness.core.llm_client import MockLLMClient

            plan_json = json.dumps(_expand_placeholders(case.mock_plan), ensure_ascii=False)
            planner = Planner(
                MockLLMClient(responses=["yes", plan_json, "Task completed"]),
                self.registry, self.store, max_plan_retries=2,
            )
            dag = DagExecutor(self.executor, self.store, self.registry)
            set_sandbox_root(str(ROOT))
            try:
                scheduler: Any = PlanningExecutorScheduler(
                    self.store, self.executor, planner, dag,
                    self.registry.list_tool_defs(), self.registry.list_tool_fns(),
                    config=config, tracer=self.tracer,
                )
                state = await self._run_with_autoconfirm(scheduler, run_id, case)
            finally:
                set_sandbox_root(str(ROOT))
            events = await self.store.get_events(run_id)
            return {"run_id": run_id, "state": state, "events": events}
        if self.llm_client is not None:
            planner = Planner(self.llm_client, self.registry, self.store, max_plan_retries=2)
            dag = DagExecutor(self.executor, self.store, self.registry)
            if case.scheduler_mode == "planning":
                scheduler: Any = PlanningExecutorScheduler(
                    self.store, self.executor, planner, dag,
                    self.registry.list_tool_defs(), self.registry.list_tool_fns(),
                    config=config, tracer=self.tracer,
                )
            else:
                from harness.core.agent_kernel import LLMAgentKernel
                kernel = LLMAgentKernel(self.llm_client)
                scheduler = AgentLoopScheduler(
                    self.store, self.executor, kernel,
                    self.registry.list_tool_defs(), self.registry.list_tool_fns(),
                    config=config, tracer=self.tracer,
                )
        else:
            # No LLM key: use a deterministic mock so the pipeline still runs.
            from harness.core.llm_client import MockLLMClient

            empty_plan = json.dumps({"intent": case.intent, "steps": []})
            planner = Planner(
                MockLLMClient(responses=[empty_plan, "Done."]),
                self.registry, self.store, max_plan_retries=2,
            )
            dag = DagExecutor(self.executor, self.store, self.registry)
            scheduler = PlanningExecutorScheduler(
                self.store, self.executor, planner, dag,
                self.registry.list_tool_defs(), self.registry.list_tool_fns(),
                config=config, tracer=self.tracer,
            )
        state = await self._run_with_autoconfirm(scheduler, run_id, case)
        events = await self.store.get_events(run_id)
        return {"run_id": run_id, "state": state, "events": events}

    @staticmethod
    def _build_mock_kernel(case: EvalCase) -> Any:
        """Build a MockAgentKernel that deterministically issues case.mock_actions.

        Each action is ``{"tool": ..., "input": {...}, "repeat": N}``; ``repeat``
        is applied for rate-limit scenarios. A trailing no-tool response lets
        the loop reach a terminal state after the scripted actions.

        ``@project@`` in an input value is expanded to the repo root so a
        scope-guardrail case can target an absolute path outside the temp
        sandbox (→ blocked) without hard-coding an absolute path.
        """
        from harness.core.agent_kernel import MockAgentKernel

        responses: list[ThinkResult] = []
        for action in case.mock_actions or []:
            tool = action["tool"]
            tool_input = _expand_placeholders(dict(action.get("input") or {}))
            for _ in range(int(action.get("repeat", 1))):
                responses.append(ThinkResult(
                    thought=f"(eval) deterministic action: {tool}",
                    tool_name=tool,
                    tool_input=tool_input,
                ))
        responses.append(ThinkResult(thought="(eval) deterministic completion"))
        return MockAgentKernel(responses)

    async def _run_with_autoconfirm(self, scheduler: Any, run_id: str, case: EvalCase) -> Any:
        """Run the scheduler, auto-confirming pending confirmations.

        The 确认流程 scenario expects the run to pause for confirmation; the
        eval then confirms so the run reaches a terminal state. Confirmation is
        only delivered after the scheduler writes RUN_PAUSED (resume() requires
        a PAUSED state), so we poll for that event and retry resume() until it
        takes effect.

        Guard: never loop forever — if the scheduler task reaches a terminal
        state (or is cancelled) without a pending confirmation, stop waiting.
        """
        if not case.expected_requires_confirmation:
            return await scheduler.run(run_id, case.intent)

        task = asyncio.create_task(scheduler.run(run_id, case.intent))
        confirmed = False
        try:
            while not task.done():
                await asyncio.sleep(0.2)
                events = await self.store.get_events(run_id)
                terminal = any(
                    e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)
                    for e in events
                )
                if terminal:
                    try:
                        return await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                    except asyncio.TimeoutError:
                        break
                if confirmed:
                    continue
                paused = any(
                    e.event_type == EventType.RUN_PAUSED
                    and e.payload.get("reason") == "waiting_confirmation"
                    for e in events
                )
                requested = [
                    e for e in events
                    if e.event_type == EventType.CONFIRMATION_REQUESTED
                ]
                if paused and requested:
                    payload = requested[-1].payload
                    await self.store.append_event(
                        run_id,
                        EventType.CONFIRMATION_RECEIVED,
                        ConfirmationReceivedPayload(
                            confirmation_id=payload["confirmation_id"],
                            confirmed=True,
                            operator_id="eval",
                        ).model_dump(),
                        idempotency_key=f"confirm_{payload['confirmation_id']}",
                    )
                    for _ in range(20):
                        ok = await scheduler.resume(run_id)
                        if ok:
                            break
                        await asyncio.sleep(0.2)
                    confirmed = True
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        return await task

    async def close(self) -> None:
        await self.store.close()


# ── HTTP mode ──────────────────────────────────────────────────────────


class _HttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def create_run(self, intent: str) -> str:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/v1/runs", json={"intent": intent})
            resp.raise_for_status()
            return resp.json()["run_id"]

    async def wait_terminal(self, run_id: str, timeout: float) -> dict[str, Any]:
        import httpx
        async with httpx.AsyncClient() as client:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                resp = await client.get(f"{self.base_url}/api/v1/runs/{run_id}")
                if resp.status_code == 404:
                    await asyncio.sleep(1.0)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data["status"] in ("completed", "failed"):
                    return data
                await asyncio.sleep(1.0)
        return {"status": "timeout"}

    async def get_events(self, run_id: str) -> list[dict[str, Any]]:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/api/v1/runs/{run_id}/events")
            resp.raise_for_status()
            return resp.json()["events"]


def _http_state_to_runstate(case: EvalCase, data: dict[str, Any], events: list[dict[str, Any]]) -> Any:
    """Reconstruct a fold-like object from HTTP JSON so scorers can consume it."""
    from harness.models.events import Event, EventType
    evs = []
    for e in events:
        try:
            et = EventType(e["event_type"])
        except ValueError:
            continue
        evs.append(Event(run_id=case.id, seq=e["seq"], event_type=et, payload=e["payload"]))
    if not evs:
        return None
    return fold_events(evs)


# ── Scoring + reporting ────────────────────────────────────────────────


def _load_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _jaccard(s1: Sequence[str], s2: Sequence[str]) -> float:
    a, b = set(s1), set(s2)
    if not a and not b:
        return 1.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 1.0


def _compute_consistency(tool_sequences: list[list[str]]) -> float:
    if len(tool_sequences) < 2:
        return 1.0
    pairs = 0
    total = 0.0
    for i in range(len(tool_sequences)):
        for j in range(i + 1, len(tool_sequences)):
            total += _jaccard(tool_sequences[i], tool_sequences[j])
            pairs += 1
    return round(total / pairs, 3) if pairs else 1.0


async def _run_with_consistency(
    case: EvalCase,
    engine: _InProcessEngine | None,
    http: _HttpClient | None,
    timeout: float,
    no_llm_judge: bool,
    tracer: LangfuseTracer | None,
    tool_names: list[str] | None,
    n_runs: int,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    sequences: list[list[str]] = []
    for i in range(n_runs):
        r = await _run_one(case, engine, http, timeout, no_llm_judge, tracer, tool_names)
        runs.append(r)
        seq = [tr["tool_name"] for tr in (r.get("_tool_results_raw") or [])]
        sequences.append(seq)
        if i < n_runs - 1:
            await asyncio.sleep(1.0)
        print(f"    [{i+1}/{n_runs}] {r['status']}  tools={seq}")

    best = max(runs, key=lambda r: r.get("duration_s", 0))
    best["consistency_score"] = _compute_consistency(sequences)
    best["consistency_runs"] = n_runs
    best["consistency_tool_sequences"] = sequences
    return best


async def _run_one(
    case: EvalCase,
    engine: _InProcessEngine | None,
    http: _HttpClient | None,
    timeout: float,
    no_llm_judge: bool,
    tracer: LangfuseTracer | None,
    tool_names: list[str] | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    if engine is not None:
        res = await asyncio.wait_for(engine.run_case(case), timeout=timeout)
        state = res["state"]
        events = res["events"]
        status = state.status.value
        summary = str(state.summary or "")
    else:
        run_id = await http.create_run(case.intent)
        data = await http.wait_terminal(run_id, timeout)
        events = await http.get_events(run_id) if data["status"] != "timeout" else []
        state = _http_state_to_runstate(case, data, events)
        status = data["status"]
        summary = str(data.get("summary") or "")

    duration = time.monotonic() - t0

    rule_scores = compute_rule_scores(case, state, events, tool_names) if state is not None else []

    result: dict[str, Any] = {
        "case_id": case.id,
        "scenario": case.scenario,
        "status": status,
        "duration_s": round(duration, 1),
        "summary": summary[:300],
        "rule_scores": {s.name: s.value for s in rule_scores},
        "_tool_results_raw": [
            {"tool_name": tr.tool_name, "status": tr.status.value}
            for tr in (state.tool_results if state else [])
        ],
    }

    if tracer is not None and tracer.enabled and state is not None:
        run_id = res["run_id"] if engine is not None else run_id
        for s in rule_scores:
            tracer.attach_score(run_id, s.name, s.value, s.comment)
        if not no_llm_judge:
            judge = LLMJudgeScorer(engine.llm_client if engine else None)
            j = await judge.score(case, state)
            if j:
                for k, v in j.items():
                    if k.startswith("_"):
                        continue
                    tracer.attach_score(run_id, k, v, "llm_judge")
                result["llm_judge"] = {k: v for k, v in j.items() if not k.startswith("_")}
    return result


async def _main(args: argparse.Namespace) -> int:
    _load_env()

    cases = DatasetLoader.load(args.dataset)
    if args.scenario:
        cases = DatasetLoader.filter_by_scenario(cases, args.scenario)
    if args.case_id:
        cases = DatasetLoader.filter_by_case_id(cases, args.case_id)
    if not cases:
        print("No cases matched the given filters.")
        return 1

    tracer = LangfuseTracer()
    print(f"Langfuse tracing: {'ENABLED' if tracer.enabled else 'DISABLED'}")

    engine = None
    http = None
    if args.api_base:
        http = _HttpClient(args.api_base)
        tool_names = None
    else:
        engine = _InProcessEngine(args.max_iterations, tracer)
        tool_names = list(engine.registry.tool_names)

    consistency = args.consistency_runs if args.consistency_runs > 1 else 0
    total = len(cases) * max(consistency, 1)
    print(f"Running {total} run(s) across {len(cases)} case(s)...\n")

    results = []
    try:
        for case in cases:
            if consistency and case.expected_tool_sequence:
                print(f"  [{case.id}] consistency mode ({consistency} runs):")
                r = await _run_with_consistency(
                    case, engine, http, args.timeout, args.no_llm_judge, tracer, tool_names, consistency,
                )
                r["rule_scores"]["consistency_score"] = r.pop("consistency_score")
                if tracer.enabled:
                    run_id = f"eval_{case.id}"
                    tracer.attach_score(run_id, "consistency_score", r["rule_scores"]["consistency_score"],
                                        f"Jaccard over {consistency} runs: {r.get('consistency_tool_sequences','')}")
            else:
                r = await _run_one(case, engine, http, args.timeout, args.no_llm_judge, tracer, tool_names)
            results.append(r)
            _print_row(r)
    finally:
        if tracer.enabled:
            await tracer.flush_async()
        if engine is not None:
            await engine.close()

    _print_summary(results)
    return 0


def _print_row(r: dict[str, Any]) -> None:
    scores = ", ".join(f"{k}={v}" for k, v in r["rule_scores"].items())
    print(f"  [{r['status']:<12}] {r['case_id']} ({r['scenario']}) {r['duration_s']}s  {scores}")


def _print_summary(results: list[dict[str, Any]]) -> None:
    print("\n==== Summary ====")
    ok = sum(1 for r in results if r["status"] == "completed")
    print(f"Completed: {ok}/{len(results)}")
    for r in results:
        print(f"  {r['case_id']}: {r['status']}")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
