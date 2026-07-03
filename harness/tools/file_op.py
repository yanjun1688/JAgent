from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from harness.models.tools import Guardrail, SideEffect, SuccessIndicator, ToolDefinition

_SANDBOX_BASE: Path | None = None


def set_sandbox_root(path: str | Path) -> None:
    global _SANDBOX_BASE
    _SANDBOX_BASE = Path(path).resolve()


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
)


async def file_op_fn(input: dict[str, Any]) -> dict[str, Any]:
    operation = input["operation"]
    path = input["path"]
    content = input.get("content")

    def _do_read() -> dict[str, Any]:
        target = _resolve_path(path)
        if not target.exists():
            return {"success": False, "path": path, "error": f"File not found: {path}"}
        if not target.is_file():
            return {"success": False, "path": path, "error": f"Not a file: {path}"}
        text = target.read_text(encoding="utf-8")
        return {"success": True, "path": path, "content": text, "size": len(text)}

    def _do_write() -> dict[str, Any]:
        target = _resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
        return {"success": True, "path": path, "size": len(content or "")}

    def _do_append() -> dict[str, Any]:
        target = _resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(content or "")
        return {"success": True, "path": path, "size": os.path.getsize(target)}

    def _do_delete() -> dict[str, Any]:
        target = _resolve_path(path)
        if not target.exists():
            return {"success": False, "path": path, "error": f"File not found: {path}"}
        if not target.is_file():
            return {"success": False, "path": path, "error": f"Not a file: {path}"}
        target.unlink()
        return {"success": True, "path": path}

    def _do_list() -> dict[str, Any]:
        target = _resolve_path(path)
        if not target.exists():
            return {"success": False, "path": path, "error": f"Path not found: {path}"}
        if target.is_file():
            return {"success": True, "path": path, "content": path, "size": os.path.getsize(target)}
        entries = sorted(
            str(e.relative_to(_SANDBOX_BASE or Path.cwd())) for e in target.iterdir()
        )
        return {"success": True, "path": path, "content": "\n".join(entries), "size": len(entries)}

    ops = {
        "read": _do_read,
        "write": _do_write,
        "append": _do_append,
        "delete": _do_delete,
        "list": _do_list,
    }

    impl = ops.get(operation)
    if impl is None:
        return {"success": False, "path": path, "error": f"Unknown operation: {operation}"}

    result = await asyncio.to_thread(impl)
    return result
