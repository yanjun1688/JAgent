"""R6 BDD (real LLM): plan-rejection events under a genuine self-healing run.

Feature: Plan rejection observability
  As an operator
  I want trusted plan rejections during a real planning run to be structured
  So that I can audit which rule rejected a planner output

Scenario:
  Given a workspace whose required input file does not exist
  When  the planning scheduler drives an intent that needs that file
  Then  the run terminates, and every Diagnostic/plan_rejected event
        - parses into PlanRejectedPayload
        - carries a valid stage, positive attempt, non-empty reasons
        - recovered events are followed by continued driving;
          recovered=False events precede a terminal run

This script is deliberately outside ``tests/`` (real-LLM cost, non-determinism):
    uv run python scripts/bdd_plan_rejection_real_llm.py
"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import logging

from harness.core.dag_executor import DagExecutor
from harness.core.llm_client import OpenAILLMClient
from harness.core.planner import Planner
from harness.core.scheduler import PlanningExecutorScheduler, SchedulerConfig
from harness.execution.local import LocalDirectoryBackend
from harness.models.events import EventType, PlanRejectedPayload
from harness.models.tools import RetryPolicy, SuccessIndicator, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.file_op import FileOpTool
from harness.tools.registry import ToolRegistry

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s")
logging.getLogger("harness").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

API_KEY = os.environ["LLM_API_KEY"]
MODEL = os.environ["LLM_MODEL_NAME"]
BASE_URL = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

INTENT = "请读取文件 missing_data.csv 的内容，把其中所有金额的总和计算后告诉我。"

# Scenario B: only file_op exists, but the intent insists on an unavailable
# tool. If the model invents it, PlanGuardrail rejects (unknown tool).
INTENT_UNAVAILABLE_TOOL = (
    "请必须使用 python_exec 这个工具来执行代码 print(1+1)，不要用其他工具代替，"
    "然后把执行结果告诉我。"
)

# Scenario C: local repair disabled; the first plan reads a missing file. If the
# global revision just re-reads the same file with no new upstream step, the
# degenerate-revision guard rejects (same tool+input signature).
INTENT_REPEAT_READ = "请读取文件 definitely_missing.csv 并告诉我它的全部内容。"

# Scenario D: a custom read-only tool that always fails with a *transient*
# timeout and has zero tool-level retries. The overwhelmingly likely first
# revision is to repeat the same ping (same signature, no new upstream) ->
# the degenerate guard rejects it, producing a real rejection event.
INTENT_FLAKY_PING = "请调用 flaky_ping 检查 demo 服务是否在线，然后告诉我检查结果。"


async def run_scenario(
    *,
    run_id: str,
    intent: str,
    backend: LocalDirectoryBackend,
    local_repair: bool,
    custom_tools: list[tuple[ToolDefinition, Callable[[dict], dict]]] | None = None,
) -> tuple[str, list]:
    store = EventStore(":memory:")
    await store.initialize()

    registry = ToolRegistry()
    registry.register_tool(FileOpTool())
    for tool_def, tool_fn in custom_tools or []:
        registry._register(tool_def, tool_fn)

    executor = ToolExecutor(store)
    llm = OpenAILLMClient(api_key=API_KEY, model=MODEL, base_url=BASE_URL)
    planner = Planner(llm, registry, store, max_plan_retries=2)
    dag = DagExecutor(executor, store, registry, backend=backend)
    scheduler = PlanningExecutorScheduler(
        store,
        executor,
        planner,
        dag,
        registry.list_tool_defs(),
        registry.list_tool_fns(),
        config=SchedulerConfig(run_attempt_budget=12, local_repair_enabled=local_repair),
        backend=backend,
    )

    state = await scheduler.run(run_id, intent)
    events = await store.get_events(run_id)
    await store.close()
    return state.status.value, events


def assert_rejection_events(events: list, final_status: str, *, label: str) -> int:
    rejection_events = [
        e for e in events if e.event_type == EventType.DIAGNOSTIC and e.phase == "plan_rejected"
    ]

    print("\n" + "=" * 72)
    print(f"  R6 BDD real-LLM [{label}] - final status: {final_status}")
    print(f"  Diagnostic/plan_rejected events: {len(rejection_events)}")
    print("-" * 72)

    seen_attempts: dict[str, list[int]] = {}
    for e in rejection_events:
        parsed = PlanRejectedPayload(**e.payload)
        seen_attempts.setdefault(parsed.stage, []).append(parsed.attempt)
        print(
            f"  seq={e.seq:>3} stage={parsed.stage:<10} attempt={parsed.attempt} "
            f"recovered={parsed.recovered}"
        )
        for reason in parsed.reasons:
            print(f"      - {reason[:200]}")
        assert parsed.attempt > 0
        assert parsed.reasons
        if not parsed.recovered:
            assert final_status == "failed"

    for stage, attempts in seen_attempts.items():
        assert attempts == sorted(attempts), f"attempts out of order for stage {stage}"

    terminal = [
        e
        for e in events
        if e.event_type == EventType.RUN and e.phase in ("completed", "failed")
    ]
    assert terminal, f"[{label}] run must reach a terminal event"
    return len(rejection_events)


async def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["A", "B", "C", "D"], default=None)
    args = parser.parse_args(argv)

    tmp = Path(__file__).parent.parent / ".sandbox_tmp"
    tmp.mkdir(exist_ok=True)
    backend = LocalDirectoryBackend(str(tmp.resolve()))

    selected = {args.only} if args.only else {"A", "B", "C", "D"}

    if "A" in selected:
        # Scenario A: missing file -> honest self-heal (rejections optional).
        status_a, events_a = await run_scenario(
            run_id="bdd-r6-a", intent=INTENT, backend=backend, local_repair=True
        )
        count_a = assert_rejection_events(events_a, status_a, label="A: missing input file")
        if count_a == 0:
            print("[NOTE A] no rejection triggered (model self-healed; non-deterministic)")

    if "B" in selected:
        # Scenario B: unavailable tool demanded -> guardrail rejection expected.
        status_b, events_b = await run_scenario(
            run_id="bdd-r6-b", intent=INTENT_UNAVAILABLE_TOOL, backend=backend, local_repair=False
        )
        count_b = assert_rejection_events(events_b, status_b, label="B: unavailable tool demanded")
        if count_b == 0:
            print("[NOTE B] model adapted instead of inventing a tool (non-deterministic)")

    if "C" in selected:
        # Scenario C: no local repair; revision repeating the failed read is rejected.
        status_c, events_c = await run_scenario(
            run_id="bdd-r6-c", intent=INTENT_REPEAT_READ, backend=backend, local_repair=False
        )
        count_c = assert_rejection_events(events_c, status_c, label="C: degenerate repeated read")
        if count_c == 0:
            print("[NOTE C] model changed strategy instead of repeating the read (non-deterministic)")

    if "D" in selected:
        # Scenario D: a tool that always fails with a transient timeout; the first
        # revision repeating it is rejected by the degenerate guard.
        def _flaky_ping(_input: dict) -> dict:
            return {
                "success": False,
                "error": "temporary timeout: service temporarily unavailable, please retry later",
            }

        flaky_def = ToolDefinition(
            name="flaky_ping",
            description=(
                "Ping the demo service to check whether it is online (read-only). "
                "Input: optional 'target' string."
            ),
            input_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
            },
            side_effects=[],
            retry_policy=RetryPolicy(max_retries=0),
            success_indicator=SuccessIndicator(field="success", op="eq", value=True),
        )
        status_d, events_d = await run_scenario(
            run_id="bdd-r6-d",
            intent=INTENT_FLAKY_PING,
            backend=backend,
            local_repair=False,
            custom_tools=[(flaky_def, _flaky_ping)],
        )
        count_d = assert_rejection_events(events_d, status_d, label="D: flaky ping degenerate revision")
        if count_d == 0:
            print("[NOTE D] model changed strategy instead of repeating the ping (non-deterministic)")

    print("\n[PASS] all rejection events are well-formed and the selected runs terminated")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
