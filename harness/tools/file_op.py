from __future__ import annotations

from harness.core.logger import guard_logger
from harness.models.tools import (
    Guardrail,
    SideEffect,
    SuccessIndicator,
    ToolScopeTarget,
)
from harness.tools.base import BaseTool, operation

_log = guard_logger("tool.file_op")


class FileOpTool(BaseTool):
    """file_op 声明式实现（ADR-010 D-01/D-03）— 唯一实现的 file_op 工具。

    ``needs_backend=True``：run 级 backend 由 executor 经 contextvar 注入
    （invoker 读取），无 backend 时 fail-closed。文件访问统一经受信
    ExecutionBackend 执行，模块内不保留任何全局沙盒根（v3.3 方案 A 根治）。
    """

    name = "file_op"
    description = "Read, write, append, or delete files within the sandbox directory."
    input_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "write", "append", "delete", "list"],
                "description": "File operation to perform",
            },
            "path": {"type": "string", "description": "File path relative to sandbox root"},
            "content": {"type": "string", "description": "Content to write or append (required for write/append)"},
        },
        "required": ["operation", "path"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "size": {"type": "integer"},
            "error": {"type": "string"},
        },
    }
    operation_key = "operation"
    side_effects = [SideEffect.WRITE, SideEffect.DELETE]
    idempotency_key_fields = ["operation", "path", "content"]
    guardrails = [
        Guardrail(guardrail_type="destructive", config={}),
        Guardrail(guardrail_type="scope", config={}),
    ]
    timeout_ms = 30000
    success_indicator = SuccessIndicator(field="success", op="eq", value=True)
    scope_targets = [ToolScopeTarget(kind="path", input_field="path")]
    needs_backend = True

    @operation("read", probe_allowed=True, ref_allowed_fields={"path": False, "content": False})
    async def read(self, input):
        return await self.backend.read(input["path"])

    @operation("list", probe_allowed=True, ref_allowed_fields={"path": False})
    async def list(self, input):
        return await self.backend.list(input["path"])

    @operation(
        "write",
        side_effects=[SideEffect.WRITE],
        ref_allowed_fields={"path": False, "content": False},
    )
    async def write(self, input):
        return await self.backend.write(input["path"], input.get("content") or "")

    @operation(
        "append",
        side_effects=[SideEffect.WRITE],
        ref_allowed_fields={"path": False, "content": False},
    )
    async def append(self, input):
        return await self.backend.append(input["path"], input.get("content") or "")

    @operation("delete", side_effects=[SideEffect.DELETE], ref_allowed_fields={"path": False})
    async def delete(self, input):
        return await self.backend.delete(input["path"])

    async def run(self, input):
        if self.backend is None:
            raise RuntimeError("Execution backend is required for file_op")
        return await super().run(input)
