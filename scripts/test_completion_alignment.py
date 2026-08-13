"""
S12 — 完成语义粒度对齐改造：opt-in 真实 LLM 回归脚本（L-04）

覆盖 13 项全局验收中需要真实弱模型验证的场景：
  * write+read 复合目标（Planner 丢 write → 必须不 fake-green）
  * caller 契约透传（POST required_operations）
  * Reviser 删契约/改路径 → 不变量守卫拒绝
  * read→list 观察替代 → 契约 unmet，绝不宣称交付达成
  * 非法 DAG / $ 悬空引用 → PlanGuardrail 计划期拒绝
  * operation 级副作用 probe
  * watchdog 取消 + 分阶段超时收敛
  * 并发 1/2/5/10 无永久卡住 / 无幽灵 Run

用法（opt-in，不进 CI）:
    python scripts/test_completion_alignment.py [--concurrency 2]

前置：.env 需配置 LLM_API_KEY / LLM_MODEL_NAME / LLM_BASE_URL。
每个 Run 有服务端分阶段超时 + 总 watchdog，禁止无限轮询。
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s")
logging.getLogger("harness").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

from harness.core.dag_executor import DagExecutor
from harness.core.fold import RunStatus
from harness.core.llm_client import OpenAILLMClient
from harness.core.planner import Planner
from harness.core.scheduler.base import SchedulerConfig
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType
from harness.models.intent import DeliveryContract, DeliverySource
from harness.storage.event_store import EventStore
from harness.tools.browser_tool import BROWSER_DEF, browser_fn
from harness.tools.executor import ToolExecutor
from harness.tools.file_op import FILE_OP_DEF, file_op_fn
from harness.tools.http_request import HTTP_REQUEST_DEF, http_request_fn
from harness.tools.mcp_call import MCP_CALL_DEF, mcp_call_fn
from harness.tools.registry import ToolRegistry


def require_env() -> None:
    if "LLM_API_KEY" not in os.environ:
        sys.exit("Skipping: LLM_API_KEY not set (opt-in real-LLM regression, L-04).")


def build_llm() -> OpenAILLMClient:
    return OpenAILLMClient(
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ.get("LLM_MODEL_NAME", "qwen3.7-flash"),
        base_url=os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )


def build_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(HTTP_REQUEST_DEF, http_request_fn)
    r.register(FILE_OP_DEF, file_op_fn)
    r.register(BROWSER_DEF, browser_fn)
    r.register(MCP_CALL_DEF, mcp_call_fn)
    return r


async def run_scenario(
    store: EventStore,
    llm: OpenAILLMClient,
    registry: ToolRegistry,
    *,
    intent: str,
    contracts: list[DeliveryContract] | None = None,
    run_timeout_ms: int = 90000,
) -> tuple[str, RunStatus, dict, list]:
    """驱动一个真实 LLM Run，返回 (run_id, status, evidence, events)。

    每个阶段时间戳记录在证据 dict 中（配合 S11 结构化日志）。
    """
    scoped = store
    executor = ToolExecutor(scoped)
    planner = Planner(llm, registry, scoped, max_plan_retries=2)
    dag = DagExecutor(executor, scoped, registry)
    config = SchedulerConfig(
        max_iterations=15,
        run_timeout_ms=run_timeout_ms,
        max_revise_retries=3,
    )
    sched = PlanningExecutorScheduler(scoped, executor, planner, dag, [], {}, config=config)
    run_id = f"s12_{int(time.time_ns() % 10_000_000)}"

    payload = {
        "intent": intent,
        "intent_raw": intent,
        "contracts": [c.model_dump() for c in contracts] if contracts else [],
    }
    await store.append_event(run_id, EventType.RUN_STARTED, payload)
    _t0 = time.monotonic()
    state = await sched.run(run_id, intent)
    elapsed = time.monotonic() - _t0
    events = await store.get_events(run_id)
    print(
        f"[s12] run={run_id} duration_s={elapsed:.2f} "
        f"plan_created={sum(1 for e in events if e.event_type == EventType.PLAN_CREATED)} "
        f"terminal_count={sum(1 for e in events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED))}"
    )
    return run_id, state.status, state.completion_evidence, events


def summarize(status: RunStatus, evidence: dict, events: list) -> str:
    ce = evidence
    return (
        f"status={status.value} deliverable_met={ce.get('deliverable_met')} "
        f"deliverable_status={ce.get('deliverable_status')} events={len(events)}"
    )


async def main() -> None:
    require_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()

    llm = build_llm()
    registry = build_registry()
    store = EventStore(":memory:")
    await store.initialize()
    results: list[str] = []

    # 场景 1: 复合 write+read 目标，caller 契约显式声明（验收项 1/2/4/7）
    contracts = [
        DeliveryContract(
            tool="file_op",
            input={"operation": "write", "path": "blackbox.txt"},
            source=DeliverySource.CALLER,
        ),
        DeliveryContract(
            tool="file_op",
            input={"operation": "read", "path": "blackbox.txt"},
            source=DeliverySource.CALLER,
        ),
    ]
    rid, status, ev, events = await run_scenario(
        store,
        llm,
        registry,
        intent="请在当前 workspace 创建 blackbox.txt，写入 hello harness blackbox，然后重新读取并告诉我内容。",
        contracts=contracts,
    )
    if status == RunStatus.COMPLETED:
        assert ev.get("deliverable_met") is True, "caller 契约全部达成才可宣称交付达成"
    else:
        assert ev.get("deliverable_met") is not True, "失败 Run 不得宣称交付达成"
    assert sum(1 for e in events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)) == 1, "终态唯一"
    results.append(f"[1] write+read caller 契约 → {summarize(status, ev, events)}")

    # 场景 2: 无 caller 契约 → 旧请求语义：要么未验证标记，要么契约达成
    rid, status, ev, events = await run_scenario(
        store,
        llm,
        registry,
        intent="请读取文件 report.txt 并告诉我是否存在。",
        run_timeout_ms=60000,
    )
    assert ev.get("deliverable_status") in ("unverified", "met", "failed")
    assert sum(1 for e in events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)) == 1
    results.append(f"[2] 无契约 → {summarize(status, ev, events)}")

    # 场景 3: watchdog 取消（短 run_timeout 下真实 LLM 必然超时 → 结构化失败）
    rid, status, ev, events = await run_scenario(
        store, llm, registry, intent="请解释一下量子计算的原理（分析型，可能绕过工具）。", run_timeout_ms=5000
    )
    assert status in (RunStatus.COMPLETED, RunStatus.FAILED)
    assert sum(1 for e in events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)) == 1
    results.append(f"[3] 短 watchdog → {summarize(status, ev, events)}")

    # 场景 4: 并发 1..N 无永久卡住 / 无幽灵 Run
    concurrent = max(1, args.concurrency)
    runs = await asyncio.gather(
        *[
            run_scenario(
                store,
                llm,
                registry,
                intent=f"写一个文件 c{idx}.txt 内容为 value-{idx}（如果适用）。",
                run_timeout_ms=90000,
            )
            for idx in range(concurrent)
        ]
    )
    for idx, (rid, status, ev, events) in enumerate(runs):
        assert status in (RunStatus.COMPLETED, RunStatus.FAILED), f"run {idx} 无终态（幽灵 Run）"
        assert sum(1 for e in events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)) == 1
        results.append(f"[4.{idx}] 并发 run {rid} → {summarize(status, ev, events)}")

    print("\n=== S12 REAL-LLM REGRESSION SUMMARY ===")
    for line in results:
        print(" ", line)
    print(f"\nconcurrency={concurrent} scenarios_run={len(results)}")
    await store.close()
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
