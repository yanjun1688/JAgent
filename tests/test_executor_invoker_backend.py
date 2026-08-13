"""Feature: executor 经 contextvar 注入 run 级 backend（ADR-010 D-03 / §7.1）

行为分层（Given/When/Then）：
  1. needs_backend 的 BaseTool 经 register_tool 注册 → executor.execute 执行 → backend 注入
  2. executor 不再按工具名特判 partial（对所有工具统一 Sandbox.invoke(invoker, input)）
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect
from harness.storage.event_store import EventStore
from harness.tools.base import BaseTool
from harness.tools.executor import ExecutionStatus, ToolExecutor
from harness.tools.registry import ToolRegistry


class _FakeBackend:
    def __init__(self):
        self.mark = "backend"

    async def resolve(self, path: str) -> str:
        return path


class _BackendProbeTool(BaseTool):
    """声明 needs_backend，run 时报告 backend 是否注入。"""

    name = "backend_probe"
    description = "Probe backend injection"
    input_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    output_schema = {"type": "object", "properties": {"backend_set": {"type": "boolean"}}}
    side_effects = [SideEffect.EXTERNAL]
    needs_backend = True

    async def run(self, input):
        return {"backend_set": self.backend is not None, "mark": getattr(self.backend, "mark", None)}


@pytest.mark.asyncio
async def test_given_needs_backend_tool_when_execute_then_backend_injected():
    # Given 一个 needs_backend 工具注册进 registry
    store = EventStore(":memory:")
    await store.initialize()
    registry = ToolRegistry()
    tool = _BackendProbeTool()
    registry.register_tool(tool)
    executor = ToolExecutor(store)
    backend = _FakeBackend()
    td = registry.get_tool_def("backend_probe")
    fn = registry.get_tool_fn("backend_probe")
    # When executor.execute 执行（携带 backend）
    result = await executor.execute("r1", "backend_probe", {"x": "1"}, td, fn, backend=backend)
    # Then backend 经 contextvar → invoker → 工具实例注入
    assert result.status == ExecutionStatus.COMPLETED
    assert result.output["backend_set"] is True
    assert result.output["mark"] == "backend"
    await store.close()


@pytest.mark.asyncio
async def test_given_no_backend_tool_when_execute_then_no_backend_required():
    # Given 无 backend 需求的工具注册
    store = EventStore(":memory:")
    await store.initialize()
    registry = ToolRegistry()
    tool = _NoBackendTool()
    registry.register_tool(tool)
    executor = ToolExecutor(store)
    td = registry.get_tool_def("no_backend_tool")
    fn = registry.get_tool_fn("no_backend_tool")
    # When executor.execute 不携带 backend
    result = await executor.execute("r2", "no_backend_tool", {"x": "1"}, td, fn)
    # Then 正常完成，无 backend 特判需求
    assert result.status == ExecutionStatus.COMPLETED
    assert result.output == {"ok": True}
    await store.close()


class _NoBackendTool(BaseTool):
    name = "no_backend_tool"
    description = "No backend"
    input_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    side_effects = [SideEffect.EXTERNAL]

    async def run(self, input):
        return {"ok": True}
