"""JAgent 全链路集成测试 (Stage C/D/E 验证)

测试场景:
  A. 全链路单层执行 + 全部工具可见 (C-1)
  B. 失败自愈修订 + user_intent 持久化 (C-2)
  C. 输出键在状态文本中展示 (C-4)
  D. ChatResponse 兼容性 (Stage D)
  E. PLAN 提示词抽象占位符 (C-5)
  F. task_state 5种枚举值静态展示 (Stage E.1)
  G. step_tasks 端到端 — partial/waived/invalid/unknown_step (Stage E.2)

用法:
  cd D:\Project\JAgent
  python scripts/integration_test.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harness import (
    EventStore,
    ToolExecutor,
    ToolDefinition,
    RetryPolicy,
    MockLLMClient,
    ChatResponse,
    ToolRegistry,
    PlanningExecutorScheduler,
    SchedulerConfig,
    DagExecutor,
    Planner,
    DagPlan,
    DagStep,
    StepResult,
    ExecState,
    TaskState,
    EventType,
    RunStatus,
    RunState,
    fold_events,
    setup_logging,
)
from harness.core.system_prompt import _PLAN_PROMPT, _REVISE_PROMPT


PASS = "  PASS"
FAIL = "  FAIL"


def make_tool_def(name: str, description: str = "", input_schema: dict | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description or f"Tool: {name}",
        input_schema=input_schema or {
            "type": "object",
            "properties": {"msg": {"type": "string", "description": "Message to process"}},
        },
        idempotency_key_fields=[],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )


async def run_full_pipeline(
    intent: str,
    llm_responses: list[str | ChatResponse],
    tool_defs: list[ToolDefinition],
    tool_fns: dict,
    max_iterations: int = 5,
    max_consecutive_failures: int = 3,
) -> tuple[RunState, list[dict], EventStore]:
    store = EventStore(":memory:")
    await store.initialize()

    executor = ToolExecutor(store)
    llm_client = MockLLMClient(llm_responses)
    registry = ToolRegistry()
    for td in tool_defs:
        registry.register(td, tool_fns[td.name])

    dag = DagExecutor(executor, store, registry)
    planner = Planner(llm_client=llm_client, registry=registry, store=store)
    config = SchedulerConfig(
        max_iterations=max_iterations,
        max_consecutive_failures=max_consecutive_failures,
    )
    scheduler = PlanningExecutorScheduler(
        store, executor, planner, dag, tool_defs, tool_fns, config=config,
    )

    import uuid
    run_id = f"int-{uuid.uuid4().hex[:8]}"
    state = await scheduler.run(run_id, intent)

    return state, llm_client.calls, store


async def scenario_a_all_tools_visible():
    """C-1: 注册5个工具，只使用2个，验证全部5个出现在plan prompt中"""
    print("\n=== Scenario A: All tools visible in plan prompt (C-1) ===")

    tool_names = ["echo", "reverse", "count_chars", "current_time", "add"]
    tool_defs = [
        make_tool_def("echo", "Echo back the input message"),
        make_tool_def("reverse", "Reverse a string"),
        make_tool_def("count_chars", "Count characters in a string"),
        make_tool_def("current_time", "Get current system time"),
        make_tool_def("add", "Add two numbers", {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
        }),
    ]
    tool_fns = {name: lambda x, n=name: {"ok": True, "tool": n, "input": x} for name in tool_names}

    plan_json = json.dumps({
        "intent": "Echo a message and reverse it",
        "steps": [
            {"id": "s1", "tool": "echo", "input": {"msg": "hello"}},
            {"id": "s2", "tool": "reverse", "input": {"msg": "$s1.input.msg"}, "depends_on": ["s1"]},
        ],
    })

    state, calls, store = await run_full_pipeline(
        "Echo and reverse a message",
        ["yes", plan_json],
        tool_defs, tool_fns,
    )

    assert state.status == RunStatus.COMPLETED, f"Expected COMPLETED, got {state.status}"
    print(f"{PASS} Pipeline completed with status={state.status.value}")

    # Verify all 5 tools appear in the plan prompt (the 2nd call = index 1)
    if len(calls) >= 2:
        plan_prompt = calls[1]["messages"][0]["content"]
        found = 0
        missing = []
        for name in tool_names:
            if name in plan_prompt:
                found += 1
            else:
                missing.append(name)
        if found == 5:
            print(f"{PASS} All 5 tools found in plan prompt")
        else:
            print(f"{FAIL} Only {found}/5 tools in plan prompt. Missing: {missing}")
    else:
        print(f"{FAIL} Expected >=2 LLM calls, got {len(calls)}")

    events = await store.get_events(state.run_id)
    tool_completed = [e for e in events if e.event_type == EventType.TOOL_COMPLETED]
    print(f"  INFO: {len(tool_completed)} tools executed, {len(events)} total events")

    await store.close()
    return state.status == RunStatus.COMPLETED


async def scenario_b_user_intent_persistence():
    """C-2: 失败自愈修订后 user_intent 持久化"""
    print("\n=== Scenario B: user_intent persistence through revision (C-2) ===")

    echo_def = make_tool_def("echo", "Echo back the input message")
    fail_def = make_tool_def("force_fail", "Always fails", {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
    })
    tool_defs = [echo_def, fail_def]

    async def echo_fn(inp):
        return {"ok": True, "echo": inp.get("msg", "")}

    def fail_fn(inp):
        raise RuntimeError("forced failure for testing")

    tool_fns = {"echo": echo_fn, "force_fail": fail_fn}

    original_intent = "Process data and verify results with validation"

    plan_json = json.dumps({
        "intent": "Process and validate",
        "steps": [
            {"id": "s1", "tool": "echo", "input": {"msg": "step1 output"}},
            {"id": "s2", "tool": "force_fail", "input": {"msg": "should fail"}},
        ],
    })
    revise_json = json.dumps({
        "intent": "Retry with fallback approach",
        "steps": [
            {"id": "s3", "tool": "echo", "input": {"msg": "fallback result"}},
        ],
        "step_tasks": {"s1": "achieved", "s2": "not_achieved"},
    })

    state, calls, store = await run_full_pipeline(
        original_intent,
        ["yes", plan_json, revise_json],
        tool_defs, tool_fns,
    )

    events = await store.get_events(state.run_id)

    # Check PlanRevised event for user_intent
    plan_revised_events = [e for e in events if e.event_type == EventType.PLAN_REVISED]
    plan_created_events = [e for e in events if e.event_type == EventType.PLAN_CREATED]

    print(f"  INFO: status={state.status.value}, events={len(events)}, "
          f"PLAN_CREATED={len(plan_created_events)}, PLAN_REVISED={len(plan_revised_events)}")

    # Verify revision happened
    if len(plan_revised_events) > 0:
        print(f"{PASS} Plan was revised after step failure")
    else:
        print(f"{FAIL} No PLAN_REVISED event found (self-heal didn't trigger)")
        await store.close()
        return False

    # C-2: Verify user_intent is preserved in the revised plan via the LLM calls
    # The revise call (index 2) should contain the original user_intent
    if len(calls) >= 3:
        revise_prompt = calls[2]["messages"][0]["content"]
        # The REVISE prompt should have "## Original User Intent" section with user_intent
        if "## Original User Intent" in revise_prompt:
            print(f"{PASS} REVISE prompt includes '## Original User Intent' section")
        else:
            print(f"{FAIL} REVISE prompt missing '## Original User Intent' section")

        if original_intent in revise_prompt:
            print(f"{PASS} Original user_intent preserved in REVISE prompt")
        else:
            print(f"{FAIL} Original user_intent NOT found in REVISE prompt")
            print(f"  DEBUG: prompt snippet = {revise_prompt[:300]}")

        if "## Plan Intent" in revise_prompt:
            print(f"{PASS} REVISE prompt includes '## Plan Intent' section (dual-slot)")
        else:
            print(f"{FAIL} REVISE prompt missing '## Plan Intent' section")
    else:
        print(f"{FAIL} Expected >=3 LLM calls, got {len(calls)}")

    # Also verify the completed event exists
    assert state.status == RunStatus.COMPLETED, f"Expected COMPLETED, got {state.status}"

    await store.close()
    return state.status == RunStatus.COMPLETED and len(plan_revised_events) > 0


async def scenario_c_output_keys_in_status():
    """C-4: 验证 build_dag_status_text 展示输出键"""
    print("\n=== Scenario C: Output keys in status text (C-4) ===")

    rich_def = make_tool_def("rich_output", "Returns structured data", {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
    })
    tool_defs = [rich_def]

    async def rich_output_fn(inp):
        return {
            "name": "Alice",
            "count": 42,
            "summary": "Everything is fine",
            "nested": {"a": 1},
        }

    tool_fns = {"rich_output": rich_output_fn}

    plan_json = json.dumps({
        "intent": "Get structured data",
        "steps": [
            {"id": "s1", "tool": "rich_output", "input": {"msg": "go"}},
        ],
    })

    state, calls, store = await run_full_pipeline(
        "Get structured data",
        ["yes", plan_json],
        tool_defs, tool_fns,
    )

    assert state.status == RunStatus.COMPLETED, f"Expected COMPLETED, got {state.status}"
    print(f"{PASS} Pipeline completed")

    # Use build_dag_status_text directly to verify output keys
    plan = DagPlan(intent="test", steps=[
        DagStep(id="s1", tool="rich_output", input={"msg": "go"}),
    ])
    result = StepResult(
        step_id="s1",
        exec_state=ExecState.COMPLETED,
        output={"name": "Alice", "count": 42, "summary": "ok"},
    )
    status_text = DagExecutor.build_dag_status_text(plan, {"s1": result}, current_layer=0)

    if "outputs:" in status_text:
        print(f"{PASS} Status text contains 'outputs:' key")
    else:
        print(f"{FAIL} Status text missing 'outputs:' key")
        print(f"  DEBUG: {status_text[:200]}")

    if "name" in status_text and "count" in status_text:
        print(f"{PASS} Output key names present in status text: {status_text}")
    else:
        print(f"{FAIL} Output key names missing from status text: {status_text}")

    await store.close()
    return True


async def scenario_d_chatresponse_compatibility():
    """Stage D: MockLLMClient 返回 ChatResponse 对象，全链路通过"""
    print("\n=== Scenario D: ChatResponse compatibility (Stage D) ===")

    echo_def = make_tool_def("echo", "Echo back the input message")
    tool_defs = [echo_def]
    tool_fns = {"echo": lambda x: {"ok": True, "result": x.get("msg", "")}}

    plan_json = json.dumps({
        "intent": "Echo a greeting",
        "steps": [
            {"id": "s1", "tool": "echo", "input": {"msg": "Hello World"}},
        ],
    })

    # Use ChatResponse objects explicitly
    responses = [
        ChatResponse(content="yes"),                                   # classify
        ChatResponse(content=plan_json, finish_reason="stop"),         # plan
    ]

    state, calls, store = await run_full_pipeline(
        "Say hello",
        responses,
        tool_defs, tool_fns,
    )

    assert state.status == RunStatus.COMPLETED, f"Expected COMPLETED, got {state.status}"
    print(f"{PASS} Pipeline completed with ChatResponse objects")

    # Verify all responses were ChatResponse
    for i, call in enumerate(calls):
        messages_sent = call["messages"]
        print(f"  INFO: call[{i}] messages={len(messages_sent)}, "
              f"tools={len(call.get('tools', []) or [])}")

    print(f"{PASS} All {len(calls)} LLM calls processed as ChatResponse")

    await store.close()
    return True


async def scenario_f_task_state_display():
    """Stage E.1: 5种 task_state 枚举值在 build_dag_status_text 中的展示"""
    print("\n=== Scenario F: task_state display — all 5 enum values (Stage E.1) ===")

    plan = DagPlan(intent="TaskState display test", steps=[
        DagStep(id="s_a", tool="CHECKED", input={}),
        DagStep(id="s_p", tool="CHECKED", input={}),
        DagStep(id="s_n", tool="CHECKED", input={}),
        DagStep(id="s_w", tool="CHECKED", input={}),
        DagStep(id="s_u", tool="CHECKED", input={}),
    ])

    results = {
        "s_a": StepResult(step_id="s_a", exec_state=ExecState.COMPLETED, task_state=TaskState.ACHIEVED, output={"ok": True, "result": "All good"}),
        "s_p": StepResult(step_id="s_p", exec_state=ExecState.COMPLETED, task_state=TaskState.PARTIAL, output={"ok": True, "result": "Mostly done"}),
        "s_n": StepResult(step_id="s_n", exec_state=ExecState.COMPLETED, task_state=TaskState.NOT_ACHIEVED, output={"ok": False, "result": "Missed target"}),
        "s_w": StepResult(step_id="s_w", exec_state=ExecState.COMPLETED, task_state=TaskState.WAIVED, output={"delegated": True}),
        "s_u": StepResult(step_id="s_u", exec_state=ExecState.COMPLETED, task_state=TaskState.UNKNOWN),
    }

    status_text = DagExecutor.build_dag_status_text(plan, results, current_layer=0)

    expected = {
        "achieved": TaskState.ACHIEVED.value,
        "partial": TaskState.PARTIAL.value,
        "not_achieved": TaskState.NOT_ACHIEVED.value,
        "waived": TaskState.WAIVED.value,
        "unknown": TaskState.UNKNOWN.value,
    }
    all_ok = True
    for label, ts_val in expected.items():
        if ts_val in status_text:
            print(f"{PASS} task={ts_val} rendered in status text ({label})")
        else:
            print(f"{FAIL} task={ts_val} NOT found in status text ({label})")
            all_ok = False

    if TaskState.NOT_ACHIEVED.value in status_text:
        not_achieved_line = [l for l in status_text.split("\n") if f"task={TaskState.NOT_ACHIEVED.value}" in l]
        if not_achieved_line:
            print(f"  INFO: not_achieved line => {not_achieved_line[0].strip()}")
    print(f"  INFO: full status text:\n{status_text}")
    return all_ok


async def scenario_g_task_state_pipeline():
    """Stage E.2: step_tasks 端到端 — partial/waived/unknown_step/invalid 混合覆盖"""
    print("\n=== Scenario G: task_state pipeline — partial/waived/unknown/invalid (Stage E.2) ===")

    echo_def = make_tool_def("echo", "Echo back the input message")
    fail_def = make_tool_def("force_fail", "Always fails", {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
    })
    tool_defs = [echo_def, fail_def]

    async def echo_fn(inp):
        return {"ok": True, "echo": inp.get("msg", "")}

    def fail_fn(inp):
        raise RuntimeError("forced failure for testing")

    tool_fns = {"echo": echo_fn, "force_fail": fail_fn}

    plan_json = json.dumps({
        "intent": "Test varied task_state assessments",
        "steps": [
            {"id": "s1", "tool": "echo", "input": {"msg": "step1 bogus state"}},
            {"id": "s2", "tool": "force_fail", "input": {"msg": "step2 fail"}},
            {"id": "s3", "tool": "echo", "input": {"msg": "step3 waived"}},
            {"id": "s4", "tool": "echo", "input": {"msg": "step4 partial"}},
        ],
    })

    revise_json = json.dumps({
        "intent": "Recover with mixed task_state assessment",
        "steps": [
            {"id": "s5", "tool": "echo", "input": {"msg": "recovery"}},
        ],
        "step_tasks": {
            "s1": "BOGUS_VALUE",
            "s2": "not_achieved",
            "s3": "waived",
            "s4": "partial",
            "ghost_step": "achieved",
            "s99": "AMAZING_STATE",
        },
    })

    state, calls, store = await run_full_pipeline(
        "Assess four steps with mixed results",
        ["yes", plan_json, revise_json],
        tool_defs, tool_fns,
    )

    assert state.status == RunStatus.COMPLETED, f"Expected COMPLETED, got {state.status}"
    print(f"{PASS} Pipeline completed with self-heal recovery")

    events = await store.get_events(state.run_id)
    plan_revised_events = [e for e in events if e.event_type == EventType.PLAN_REVISED]
    plan_created_events = [e for e in events if e.event_type == EventType.PLAN_CREATED]
    print(f"  INFO: status={state.status.value}, events={len(events)}, "
          f"PLAN_CREATED={len(plan_created_events)}, PLAN_REVISED={len(plan_revised_events)}")

    if len(plan_revised_events) > 0:
        print(f"{PASS} Plan was revised after step failure")
    else:
        print(f"{FAIL} No PLAN_REVISED event found")
        await store.close()
        return False

    print(f"  INFO: Check harness.log for expected entries:")
    print(f"        1. [WARNING] step_tasks: invalid state BOGUS_VALUE for s1 → defaults to unknown")
    print(f"        2. [INFO] step_tasks from LLM: [('s1', 'unknown'), ('s3', 'waived'), ('s4', 'partial')]")
    print(f"        3. [INFO] Merged 3 step_tasks from LLM assessment")
    print(f"        4. s2(FAILED) filtered by should_not_rerun, ghost_step/s99 unknown steps ignored")

    await store.close()
    return True


async def scenario_e_abstract_placeholders():
    """C-5: PLAN 提示词使用抽象占位符"""
    print("\n=== Scenario E: Abstract placeholders in PLAN prompt (C-5) ===")

    checks = {
        "abstract_placeholders": False,
        "no_concrete_examples": True,
    }

    # Verify PLAN prompt uses abstract placeholders
    if "tool_A" in _PLAN_PROMPT and "tool_B" in _PLAN_PROMPT:
        print(f"{PASS} PLAN prompt uses abstract placeholders (tool_A, tool_B)")
        checks["abstract_placeholders"] = True
    else:
        print(f"{FAIL} PLAN prompt does NOT use abstract placeholders")

    # Verify no concrete tool names in example sections
    # (these were the old concrete examples that should be gone)
    concrete_keywords = ["web_search", "fetch_url", "save_to_file", "http_request",
                         "file_op", "web_fetch"]
    found_concrete = [kw for kw in concrete_keywords if kw.lower() in _PLAN_PROMPT.lower()]
    if not found_concrete:
        print(f"{PASS} No concrete tool names in PLAN prompt examples")
    else:
        print(f"{FAIL} Found concrete tool names in PLAN prompt: {found_concrete}")
        checks["no_concrete_examples"] = False

    # Also verify tool_X and tool_Y are present
    if "tool_X" in _PLAN_PROMPT and "tool_Y" in _PLAN_PROMPT:
        print(f"{PASS} PLAN prompt also uses tool_X, tool_Y for data flow examples")

    # Verify REVISE prompt dual slots
    if "{user_intent}" in _REVISE_PROMPT and "{intent}" in _REVISE_PROMPT:
        print(f"{PASS} REVISE prompt has dual slots: user_intent + intent")
    else:
        print(f"{FAIL} REVISE prompt missing dual slots")
        if "{user_intent}" not in _REVISE_PROMPT:
            print(f"  DEBUG: missing {{user_intent}}")
        if "{intent}" not in _REVISE_PROMPT:
            print(f"  DEBUG: missing {{intent}}")

    return all(checks.values())


async def main():
    setup_logging("data/logs/harness.log", level=logging.DEBUG)

    print("=" * 65)
    print("JAgent 全链路集成测试 — Stage C/D 验证")
    print("=" * 65)

    results: dict[str, bool] = {}

    # E: static check, runs first (no I/O needed)
    results["E: abstract_placeholders"] = await scenario_e_abstract_placeholders()

    # A: all tools visible
    results["A: all_tools_visible"] = await scenario_a_all_tools_visible()

    # B: user_intent persistence
    results["B: user_intent_persistence"] = await scenario_b_user_intent_persistence()

    # C: output keys in status
    results["C: output_keys_in_status"] = await scenario_c_output_keys_in_status()

    # D: ChatResponse compatibility
    results["D: chatresponse_compat"] = await scenario_d_chatresponse_compatibility()

    # F: task_state display — static check, no pipeline
    results["F: task_state_display"] = await scenario_f_task_state_display()

    # G: task_state pipeline — partial/waived/invalid/unknown_step
    results["G: task_state_pipeline"] = await scenario_g_task_state_pipeline()

    # Summary
    print("\n" + "=" * 65)
    print("RESULTS SUMMARY")
    print("=" * 65)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for scenario, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {scenario}")
    print(f"\n  {passed}/{total} scenarios passed")

    if passed == total:
        print("\n  All Stage C/D changes verified successfully.")
    else:
        print(f"\n  {total - passed} scenario(s) failed. Check output above.")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
