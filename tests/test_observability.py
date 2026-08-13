"""S11 — 可观测性（结构化日志 / UTF-8 / 失败计数 / reload 目录）测试。

覆盖问题九（reload 风暴）、问题十（日志/状态不可靠）、AGENTS.md §6.1。
失败计数来自事件折叠并与终态一致；中文意图 UTF-8 往返；LLM 调用结构化日志。
"""

from __future__ import annotations

import logging
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path


from harness.analysis.service import AnalysisService
from harness.core.fold import ToolResultStatus, fold_events
from harness.core.llm_client import MockLLMClient
from harness.models.events import EventType, RunFailedPayload, RunStartedPayload, ToolFailedPayload, ToolTimeoutPayload
from harness.storage.event_store import EventStore


# ── UTF-8 写读闭环（问题十 1）───────────────────────────────────


def test_utf8_chinese_roundtrip_through_log_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harness.log"
        handler = RotatingFileHandler(str(path), encoding="utf-8")
        logger = logging.getLogger("harness.agent.s11test")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        original = "用户意图：创建 blackbox.txt 并写入 hello harness blackbox"
        logger.info(original)
        handler.flush()
        handler.close()
        content = path.read_text(encoding="utf-8")
        assert original in content


# ── LLM 调用结构化日志（问题八 观测侧）────────────────────────


async def test_llm_structured_log_has_phase_run_duration(caplog):
    from harness.core.planner import Planner
    from harness.models.tools import ToolDefinition
    from harness.tools.registry import ToolRegistry

    store = EventStore(":memory:")
    await store.initialize()
    try:
        registry = ToolRegistry()
        registry._register(
            ToolDefinition(name="echo", description="e", input_schema={}, side_effects=[]),
            lambda x: {"ok": True},
        )
        planner = Planner(MockLLMClient(responses=['{"steps": [{"id": "s1", "tool": "echo", "input": {}}]}']), registry)
        with caplog.at_level(logging.INFO, logger="harness.agent.planner"):
            await planner.plan("test intent")
        records = [r for r in caplog.records if r.name == "harness.agent.planner" and "[llm]" in r.getMessage()]
        assert records, "expected structured [llm] log"
        msg = records[0].getMessage()
        assert "phase=plan" in msg
        assert "duration_ms=" in msg
        assert "run=" in msg
    finally:
        await store.close()


# ── 失败计数来自事件折叠、与终态一致（问题十 4）───────────────


async def test_failure_count_from_fold_matches_terminal_state():
    store = EventStore(":memory:")
    await store.initialize()
    try:
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="t").model_dump())
        await store.append_event(
            "r1",
            EventType.TOOL_FAILED,
            ToolFailedPayload(tool_call_id="tc1", tool_name="echo", error="boom").model_dump(),
        )
        await store.append_event(
            "r1",
            EventType.TOOL_TIMEOUT,
            ToolTimeoutPayload(tool_call_id="tc2", tool_name="echo", timeout_ms=100).model_dump(),
        )
        await store.append_event("r1", EventType.RUN_FAILED, RunFailedPayload(final_error="x", event_count=3).model_dump())

        state = fold_events(await store.get_events("r1"))
        folded_failures = sum(
            1
            for tr in state.tool_results
            if tr.status in (ToolResultStatus.FAILED, ToolResultStatus.TIMEOUT, ToolResultStatus.GUARDRAIL_BLOCKED)
        )
        assert folded_failures == 2
        assert state.status.value == "failed"
        # status=failed 时 failures 不能为 0
        assert folded_failures >= 1

        dashboard = await AnalysisService(store).get_dashboard()
        assert dashboard.overview.failed_runs >= 1
        assert dashboard.overview.total_tool_failures >= 2
    finally:
        await store.close()


async def test_completed_run_failure_count_zero():
    from harness.models.events import RunCompletedPayload

    store = EventStore(":memory:")
    await store.initialize()
    try:
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="t").model_dump())
        await store.append_event(
            "r1",
            EventType.TOOL_COMPLETED,
            {
                "tool_call_id": "tc1",
                "tool_name": "echo",
                "output": {"ok": True},
                "duration_ms": 1,
                "result_type": "success",
            },
        )
        await store.append_event("r1", EventType.RUN_COMPLETED, RunCompletedPayload(result_summary="done").model_dump())
        state = fold_events(await store.get_events("r1"))
        folded_failures = sum(
            1
            for tr in state.tool_results
            if tr.status in (ToolResultStatus.FAILED, ToolResultStatus.TIMEOUT, ToolResultStatus.GUARDRAIL_BLOCKED)
        )
        assert folded_failures == 0
    finally:
        await store.close()


# ── reload 目录限定源码（问题九）───────────────────────────────


def test_reload_dirs_exclude_runtime_artifacts():
    src = Path("harness/api/serve.py").read_text(encoding="utf-8")
    assert 'reload_dirs=["harness", "frontend/src"]' in src
    for forbidden in ("data", "logs", ".db", "workspaces", "__pycache__"):
        assert f'"{forbidden}"' not in src.split("uvicorn.run", 1)[-1].split("if __name__", 1)[0], (
            f"reload_dirs must not include {forbidden}"
        )
