from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.logger import guard_logger
from harness.execution.base import ExecutionBackend
from harness.execution.local import LocalDirectoryBackend
from harness.models.tools import (
    Guardrail,
    OperationContract,
    SideEffect,
    SuccessIndicator,
    ToolDefinition,
    ToolScopeTarget,
)
from harness.tools.base import BaseTool, operation

_SANDBOX_BASE: Path | None = None
_log = guard_logger("tool.file_op")


def set_sandbox_root(path: str | Path) -> None:
    global _SANDBOX_BASE
    _SANDBOX_BASE = Path(path).resolve()


def reset_sandbox_root() -> None:
    """Reset the sandbox root to its default (cwd-based resolution).

    Primarily used by tests so a sandbox root set by one test does not leak
    into others. Mirrors the initial ``_SANDBOX_BASE = None`` state.
    """
    global _SANDBOX_BASE
    _SANDBOX_BASE = None


def _resolve_path(relative_path: str) -> Path:
    base = _SANDBOX_BASE or Path.cwd()
    target = (base / relative_path).resolve()
    if not str(target).startswith(str(base)):
        raise PermissionError(f"Path '{relative_path}' is outside the sandbox root '{base}'")
    return target


FILE_OP_DEF = ToolDefinition(
    name="file_op",
    description="Read, write, append, or delete files within the sandbox directory.",
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "write", "append", "delete", "list"],
                "description": "File operation to perform",
            },
            "path": {
                "type": "string",
                "description": "File path relative to sandbox root",
            },
            "content": {
                "type": "string",
                "description": "Content to write or append, MUST be a plain string. "
                "Do NOT use $var.field references here — resolve them first. "
                "(required for write/append)",
            },
        },
        "required": ["operation", "path"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "size": {"type": "integer"},
            "error": {"type": "string"},
        },
    },
    idempotency_key_fields=["operation", "path", "content"],
    side_effects=[SideEffect.WRITE, SideEffect.DELETE],
    guardrails=[
        Guardrail(guardrail_type="destructive", config={}),
        Guardrail(guardrail_type="scope", config={}),
    ],
    timeout_ms=30000,
    success_indicator=SuccessIndicator(field="success", op="eq", value=True),
    # ADR-010 D-04: scope 目标契约化（取代 ScopeGuardrail 名称特判）。
    scope_targets=[ToolScopeTarget(kind="path", input_field="path")],
    # S02: per-operation contracts — read/list are read-only (probe allowed);
    # write/append mutate, delete destroys (no probe).  path/content never
    # allow $step.output references (D-01).
    operations=[
        OperationContract(
            operation="read",
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "size": {"type": "integer"},
                    "error": {"type": "string"},
                },
            },
            side_effects=[],
            probe_allowed=True,
            ref_allowed_fields={"path": False, "content": False},
        ),
        OperationContract(
            operation="list",
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "path": {"type": "string"},
                    "entries": {"type": "array"},
                    "error": {"type": "string"},
                },
            },
            side_effects=[],
            probe_allowed=True,
            ref_allowed_fields={"path": False},
        ),
        OperationContract(
            operation="write",
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "path": {"type": "string"},
                    "error": {"type": "string"},
                },
            },
            side_effects=[SideEffect.WRITE],
            probe_allowed=False,
            ref_allowed_fields={"path": False, "content": False},
        ),
        OperationContract(
            operation="append",
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "path": {"type": "string"},
                    "error": {"type": "string"},
                },
            },
            side_effects=[SideEffect.WRITE],
            probe_allowed=False,
            ref_allowed_fields={"path": False, "content": False},
        ),
        OperationContract(
            operation="delete",
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "path": {"type": "string"},
                    "error": {"type": "string"},
                },
            },
            side_effects=[SideEffect.DELETE],
            probe_allowed=False,
            ref_allowed_fields={"path": False},
        ),
    ],
)


async def file_op_fn(
    input: dict[str, Any],
    *,
    backend: ExecutionBackend | None = None,
    allow_legacy_fallback: bool = False,
) -> dict[str, Any]:
    # Trusted execution paths must pass a workspace backend. The explicit
    # fallback flag exists only for legacy unit tests and is never used by the
    # ToolExecutor production path.
    if backend is None:
        if not allow_legacy_fallback:
            raise RuntimeError("Execution backend is required for file_op")
        _log.warning(
            "file_op invoked with explicit legacy test fallback at %s",
            _SANDBOX_BASE or Path.cwd(),
        )
        backend = LocalDirectoryBackend(str(_SANDBOX_BASE or Path.cwd()))
    operation = input["operation"]
    path = input["path"]
    content = input.get("content")

    if operation == "read":
        return await backend.read(path)
    if operation == "write":
        return await backend.write(path, content or "")
    if operation == "append":
        return await backend.append(path, content or "")
    if operation == "delete":
        return await backend.delete(path)
    if operation == "list":
        return await backend.list(path)
    return {"success": False, "path": path, "error": f"Unknown operation: {operation}"}


class FileOpTool(BaseTool):
    """file_op 声明式实现（ADR-010 D-01/D-03）— 取代 FILE_OP_DEF + file_op_fn。

    ``needs_backend=True``：run 级 backend 由 executor 经 contextvar 注入
    （invoker 读取），无 backend 时 fail-closed。
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
