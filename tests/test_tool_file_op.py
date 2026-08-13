"""Feature: FileOpTool 迁移为 BaseTool（ADR-010 §8.2 循环 8，file_op 提前）

行为分层（Given/When/Then）：
  1. to_definition → 合成 5 个 operation 契约、判别键 operation、path scope 目标
  2. backend 注入后 → 各 operation 调用 backend 对应方法
  3. 无 backend 时 run → RuntimeError（fail-closed）
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect, ToolScopeTarget
from harness.tools.file_op import FileOpTool


class _FakeBackend:
    async def resolve(self, path):
        return path

    async def read(self, path):
        return {"success": True, "path": path, "content": "hi", "size": 2}

    async def write(self, path, content):
        return {"success": True, "path": path, "size": len(content or "")}

    async def append(self, path, content):
        return {"success": True, "path": path}

    async def delete(self, path):
        return {"success": True, "path": path}

    async def list(self, path):
        return {"success": True, "path": path, "entries": []}


class TestDefinition:
    def test_given_file_op_declares_contract_when_to_definition_then_operations_synthesized(self):
        # Given FileOpTool 声明
        td = FileOpTool().to_definition()
        # When 合成
        ops = {o.operation: o for o in td.operations}
        # Then 5 个 operation、判别键、scope 目标齐备
        assert td.name == "file_op"
        assert td.operation_key == "operation"
        assert FileOpTool.needs_backend is True
        assert set(ops) == {"read", "write", "append", "delete", "list"}
        assert ops["read"].probe_allowed is True
        assert ops["read"].side_effects == []
        assert ops["write"].side_effects == [SideEffect.WRITE]
        assert ops["delete"].side_effects == [SideEffect.DELETE]
        assert ops["write"].required_input == []
        assert td.scope_targets == [ToolScopeTarget(kind="path", input_field="path")]


class TestBackendDispatch:
    @pytest.mark.asyncio
    async def test_given_backend_injected_when_read_then_backend_read_called(self):
        # Given backend 注入的 FileOpTool 并执行 read
        tool = FileOpTool()
        # When invoke（注入 backend + dispatch）
        result = await tool.invoke({"operation": "read", "path": "x.txt"}, backend=_FakeBackend())
        # Then 调用 backend.read
        assert result["content"] == "hi"

    @pytest.mark.asyncio
    async def test_given_backend_injected_when_write_then_backend_write_called(self):
        tool = FileOpTool()
        result = await tool.invoke(
            {"operation": "write", "path": "x.txt", "content": "hi"}, backend=_FakeBackend()
        )
        assert result["size"] == 2

    @pytest.mark.asyncio
    async def test_given_unknown_operation_then_raises(self):
        tool = FileOpTool()
        with pytest.raises(KeyError):
            await tool.invoke({"operation": "unknown"}, backend=_FakeBackend())

    @pytest.mark.asyncio
    async def test_given_no_backend_when_run_then_raises(self):
        # Given 未注入 backend
        tool = FileOpTool()
        # When run mutating/任何 operation
        with pytest.raises(RuntimeError, match="backend is required"):
            await tool.run({"operation": "write", "path": "x.txt"})
