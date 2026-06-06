"""
Harness Real-LLM Data Flow Verification

Tests all 4 production tools via natural language intents.
Pipeline observed:
  LLM -> AgentKernel -> Scheduler -> ToolExecutor -> Tool -> EventStore
  EventStore -> RunMonitor -> FeedbackInjected -> Scheduler -> AgentKernel
  ToolFailed -> state -> next think() -> Agent self-healing

Usage:
    python scripts/test_real_llm_flow.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from harness.core.agent_kernel import LLMAgentKernel
from harness.core.context_manager import ContextManager
from harness.core.llm_client import OpenAILLMClient
from harness.core.scheduler import AgentLoopScheduler, SchedulerConfig
from harness.monitoring.run_monitor import RunMonitor
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.file_op import FILE_OP_DEF, file_op_fn, set_sandbox_root
from harness.tools.http_request import HTTP_REQUEST_DEF, http_request_fn
from harness.tools.browser_tool import BROWSER_DEF, browser_fn, BrowserManager
from harness.tools.mcp_call import MCP_CALL_DEF, mcp_call_fn, connect_mcp_server, disconnect_mcp_server

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s")
logging.getLogger("harness").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

API_KEY = os.environ["LLM_API_KEY"]
MODEL = os.environ["LLM_MODEL_NAME"]
BASE_URL = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

TMP = Path(__file__).parent.parent / ".sandbox_tmp"
TMP.mkdir(exist_ok=True)
set_sandbox_root(str(TMP.resolve()))


def make_client() -> OpenAILLMClient:
    return OpenAILLMClient(api_key=API_KEY, model=MODEL, base_url=BASE_URL)


def make_monitor(store: EventStore) -> RunMonitor:
    m = RunMonitor(store, max_tokens=1, token_warning_ratio=0.5)
    m.attach()
    return m


def make_cm(store: EventStore) -> ContextManager:
    return ContextManager(store, token_limit=4000, compression_threshold_ratio=0.8, checkpoint_interval=5)


# ── Report ───────────────────────────────────────────────

def report(title: str, run_id: str, state, events: list) -> dict:
    tc = [e for e in events if e.event_type.value == "ToolCalled"]
    tok = [e for e in events if e.event_type.value == "ToolCompleted"]
    tf = [e for e in events if e.event_type.value == "ToolFailed"]
    gr = [e for e in events if e.event_type.value == "GuardrailTriggered"]
    fb = [e for e in events if e.event_type.value == "FeedbackInjected"]
    th = [e for e in events if e.event_type.value == "AgentThought"]

    rule = "=" * 72
    print(f"\n{rule}")
    print(f"  {title}")
    print(f"  Status: {state.status.value}  |  Tools: {len(tc)} called, {len(tok)} OK, {len(tf)} fail, {len(gr)} guard  |  Feedback: {len(fb)}  |  Thoughts: {len(th)}")
    print(f"{'-' * 72}")

    for e in events:
        et = e.event_type.value
        p = e.payload if isinstance(e.payload, dict) else {}

        if et == "AgentThought":
            t = p.get("thought", "")
            print(f"\n  >> AgentThought (seq={e.seq}, tool={p.get('tool_choice','')}):")
            for line in t.split("\n"):
                print(f"     | {line}")

        elif et == "ToolCalled":
            inp = json.dumps(p.get("input", {}), ensure_ascii=False)[:120]
            print(f"\n  >> ToolCalled (seq={e.seq}): {p.get('tool_name')}  input={inp}")

        elif et == "ToolCompleted":
            out = json.dumps(p.get("output", {}), ensure_ascii=False)[:120]
            print(f"  << ToolCompleted (seq={e.seq}): {p.get('tool_name')}  ({p.get('duration_ms',0)}ms)  result={out}")

        elif et == "ToolFailed":
            print(f"  << ToolFailed (seq={e.seq}): {p.get('tool_name')}  error={p.get('error','')[:200]}")

        elif et == "GuardrailTriggered":
            print(f"  << GuardrailTriggered (seq={e.seq}): {p.get('tool_name')}  rule={p.get('guardrail_id')}  reason={p.get('reason','')}")

        elif et == "FeedbackInjected":
            print(f"  << FeedbackInjected (seq={e.seq}): [{p.get('priority','')}] {p.get('feedback_text','')[:160]}")

    print(f"{rule}")

    return {
        "tool": title.split("[")[1].split("]")[0] if "[" in title else run_id,
        "status": state.status.value,
        "calls": len(tc), "ok": len(tok), "fail": len(tf), "guard": len(gr),
        "fdbk": len(fb), "thoughts": len(th),
    }


# ── Tests ────────────────────────────────────────────────

async def test_http_request() -> dict:
    """Normal: user asks to GET an API endpoint."""
    store = EventStore(":memory:"); await store.initialize()
    mon = make_monitor(store)
    cm = make_cm(store)
    sched = AgentLoopScheduler(store, ToolExecutor(store), LLMAgentKernel(make_client()),
        [HTTP_REQUEST_DEF], {"http_request": http_request_fn},
        SchedulerConfig(max_iterations=8), monitor=mon, context_manager=cm)
    state = await sched.run("http-norm", "帮我请求 httpbin.org 的 /get 接口，把返回的内容告诉我")
    events = await store.get_events("http-norm"); await store.close()
    return report("[http_request] 正常流", "http-norm", state, events)


async def test_http_request_error() -> dict:
    """Error: user asks to access a non-existent domain."""
    store = EventStore(":memory:"); await store.initialize()
    mon = make_monitor(store)
    cm = make_cm(store)
    sched = AgentLoopScheduler(store, ToolExecutor(store), LLMAgentKernel(make_client()),
        [HTTP_REQUEST_DEF], {"http_request": http_request_fn},
        SchedulerConfig(max_iterations=8), monitor=mon, context_manager=cm)
    state = await sched.run("http-err", "帮我访问一下这个网站看看能不能打开：https://a-non-existent-domain-for-testing-12345.xyz")
    events = await store.get_events("http-err"); await store.close()
    return report("[http_request] 错误流", "http-err", state, events)


async def test_file_op() -> dict:
    """Normal: user asks to create a file and read it back."""
    store = EventStore(":memory:"); await store.initialize()
    mon = make_monitor(store)
    cm = make_cm(store)
    sched = AgentLoopScheduler(store, ToolExecutor(store), LLMAgentKernel(make_client()),
        [FILE_OP_DEF], {"file_op": file_op_fn},
        SchedulerConfig(max_iterations=8), monitor=mon, context_manager=cm)
    state = await sched.run("file-norm", "帮我创建一个叫 greeting.txt 的文件，内容写上 'Hello from Harness Agent!'，然后再读取它验证")
    events = await store.get_events("file-norm"); await store.close()
    return report("[file_op] 正常流", "file-norm", state, events)


async def test_file_op_error() -> dict:
    """Error: user asks to write outside the sandbox (triggers PermissionError)."""
    store = EventStore(":memory:"); await store.initialize()
    mon = make_monitor(store)
    cm = make_cm(store)
    sched = AgentLoopScheduler(store, ToolExecutor(store), LLMAgentKernel(make_client()),
        [FILE_OP_DEF], {"file_op": file_op_fn},
        SchedulerConfig(max_iterations=8), monitor=mon, context_manager=cm)
    state = await sched.run("file-err", "帮我在 ../outside.txt 写一个文件，内容写 'test'")
    events = await store.get_events("file-err"); await store.close()
    return report("[file_op] 错误流", "file-err", state, events)


async def test_browser() -> dict:
    """Normal: user asks to visit httpbin.org and get page info."""
    store = EventStore(":memory:"); await store.initialize()
    mon = make_monitor(store)
    cm = make_cm(store)
    sched = AgentLoopScheduler(store, ToolExecutor(store), LLMAgentKernel(make_client()),
        [BROWSER_DEF], {"browser": browser_fn},
        SchedulerConfig(max_iterations=8), monitor=mon, context_manager=cm)
    try:
        state = await sched.run("browser-norm", "帮我打开 httpbin.org 这个网站，看看页面标题是什么")
        events = await store.get_events("browser-norm")
        return report("[browser] 正常流", "browser-norm", state, events)
    finally:
        await BrowserManager.cleanup()


async def test_browser_error() -> dict:
    """Error: user asks to visit a non-existent website."""
    store = EventStore(":memory:"); await store.initialize()
    mon = make_monitor(store)
    cm = make_cm(store)
    sched = AgentLoopScheduler(store, ToolExecutor(store), LLMAgentKernel(make_client()),
        [BROWSER_DEF], {"browser": browser_fn},
        SchedulerConfig(max_iterations=8), monitor=mon, context_manager=cm)
    try:
        state = await sched.run("browser-err", "帮我打开这个网站看看有什么内容：https://a-domain-that-does-not-exist-99999.com")
        events = await store.get_events("browser-err")
        return report("[browser] 错误流", "browser-err", state, events)
    finally:
        await BrowserManager.cleanup()


async def _mcp_test(run_id: str, intent: str, label: str) -> dict:
    """Run a single MCP test: connect server -> scheduler -> disconnect."""
    conn = await connect_mcp_server("test-server", command=["npx", "-y", "@modelcontextprotocol/server-everything"])
    if not conn.get("success"):
        return {"tool": "mcp_call", "status": f"MCP connect failed: {conn.get('error','?')}"}
    try:
        store = EventStore(":memory:"); await store.initialize()
        mon = make_monitor(store)
        cm = make_cm(store)
        sched = AgentLoopScheduler(store, ToolExecutor(store), LLMAgentKernel(make_client()),
            [MCP_CALL_DEF], {"mcp_call": mcp_call_fn},
            SchedulerConfig(max_iterations=8), monitor=mon, context_manager=cm)
        state = await sched.run(run_id, intent)
        events = await store.get_events(run_id)
        r = report(label, run_id, state, events)
        await store.close()
        return r
    finally:
        await disconnect_mcp_server("test-server")


async def test_mcp_call() -> dict:
    return await _mcp_test("mcp-norm",
        "帮我用 mcp_call 调用 test-server 上的 echo 工具，发送消息 'hello from harness'",
        "[mcp_call] 正常流")


async def test_mcp_call_error() -> dict:
    return await _mcp_test("mcp-err",
        "帮我用 mcp_call 调用 test-server 上一个不存在的工具 non_existent_tool_xyz",
        "[mcp_call] 错误流")


# ── Main ────────────────────────────────────────────────

async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"\n{'#' * 72}")
    print(f"#  Harness Real-LLM Data Flow Verification")
    print(f"#  Model: {MODEL}  |  Tools: http_request, file_op, browser, mcp_call")
    print(f"#  Each tool tested with natural language: normal flow + error flow")
    print(f"{'#' * 72}")

    tests: list[tuple[str, asyncio.coroutine]] = [
        ("http_request normal", test_http_request()),
        ("http_request error",  test_http_request_error()),
        ("file_op normal",      test_file_op()),
        ("file_op error",       test_file_op_error()),
        ("browser normal",      test_browser()),
        ("browser error",       test_browser_error()),
        ("mcp_call normal",     test_mcp_call()),
        ("mcp_call error",      test_mcp_call_error()),
    ]

    results = []
    for name, coro in tests:
        try:
            r = await coro
            results.append(r)
        except Exception as exc:
            print(f"\n  [EXCEPTION] {name}: {exc}")
            import traceback; traceback.print_exc()
            results.append({"tool": name, "status": f"EXCEPTION: {exc}"})

    shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{'=' * 72}")
    print(f"  DATA FLOW TEST SUMMARY")
    print(f"{'=' * 72}")
    hdr = f"  {'Tool':<28s} {'Status':<14s} {'Call':>4s} {'OK':>4s} {'Fail':>4s} {'Guard':>5s} {'Fdbk':>4s}"
    print(hdr)
    print(f"  {'-' * (len(hdr.strip()))}")
    for r in results:
        s = r.get("status", "?")
        if len(s) > 13: s = s[:13]
        print(f"  {r.get('tool','?'):<28s} {s:<14s} "
              f"{r.get('calls',0):>4d} {r.get('ok',0):>4d} {r.get('fail',0):>4d} "
              f"{r.get('guard',0):>5d} {r.get('fdbk',0):>4d}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
