from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harness.execution.base import ExecutionBackend


class LocalDirectoryBackend(ExecutionBackend):
    def __init__(self, filesystem_root: str) -> None:
        self._root = Path(filesystem_root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> str:
        return str(self._root)

    async def resolve(self, path: str) -> str:
        if not isinstance(path, str) or not path:
            raise PermissionError("path must be a non-empty string")

        def _resolve() -> str:
            candidate = Path(path)
            if candidate.is_absolute():
                raise PermissionError("absolute paths are not allowed")
            target = (self._root / candidate).resolve(strict=False)
            try:
                target.relative_to(self._root)
            except ValueError as exc:
                raise PermissionError(f"Path '{path}' is outside the sandbox workspace root") from exc
            return str(target)

        return await asyncio.to_thread(_resolve)

    async def read(self, path: str) -> dict[str, Any]:
        target = Path(await self.resolve(path))
        if not target.is_file():
            return {"success": False, "path": path, "error": f"File not found: {path}"}
        content = await asyncio.to_thread(target.read_text, encoding="utf-8")
        return {"success": True, "path": path, "content": content, "size": len(content)}

    async def write(self, path: str, content: str) -> dict[str, Any]:
        target = Path(await self.resolve(path))
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
        return {"success": True, "path": path, "size": len(content)}

    async def append(self, path: str, content: str) -> dict[str, Any]:
        target = Path(await self.resolve(path))
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)

        def _append() -> int:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(content)
            return target.stat().st_size

        return {"success": True, "path": path, "size": await asyncio.to_thread(_append)}

    async def delete(self, path: str) -> dict[str, Any]:
        target = Path(await self.resolve(path))
        if not target.is_file():
            return {"success": False, "path": path, "error": f"File not found: {path}"}
        await asyncio.to_thread(target.unlink)
        return {"success": True, "path": path}

    async def list(self, path: str) -> dict[str, Any]:
        target = Path(await self.resolve(path))
        if not target.exists():
            return {"success": False, "path": path, "error": f"Path not found: {path}"}
        if target.is_file():
            return {"success": True, "path": path, "content": path, "size": target.stat().st_size}
        entries = sorted(str(entry.relative_to(self._root)).replace("\\", "/") for entry in target.iterdir())
        return {"success": True, "path": path, "content": "\n".join(entries), "size": len(entries)}
