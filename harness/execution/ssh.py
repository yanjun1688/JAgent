from __future__ import annotations

import asyncio
from typing import Any

from harness.execution.base import ExecutionBackend


class RemoteUnavailableError(RuntimeError):
    pass


class RemoteSSHBackend(ExecutionBackend):
    """Explicit placeholder until the optional SSH dependency is installed."""

    def __init__(self, host: str, port: int, username: str, key_path: str, root: str) -> None:
        self.host, self.port, self.username, self.key_path, self._root = host, port, username, key_path, root
        self._client = None
        self._sftp = None

    @property
    def root(self) -> str:
        return self._root

    async def resolve(self, path: str) -> str:
        if not path or path.startswith("/") or ".." in path.replace("\\", "/").split("/"):
            raise PermissionError("path escapes SSH workspace root")
        return f"{self._root.rstrip('/')}/{path.replace(chr(92), '/')}"

    async def _ensure(self):
        if self._sftp is not None:
            return self._sftp

        def _connect():
            try:
                import paramiko  # type: ignore[import-untyped]
            except ImportError as exc:
                raise RemoteUnavailableError("paramiko is required for SSH workspaces") from exc
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(self.host, port=self.port, username=self.username, key_filename=self.key_path)
            self._client = client
            self._sftp = client.open_sftp()
            return self._sftp

        return await asyncio.to_thread(_connect)

    async def read(self, path: str) -> dict[str, Any]:
        sftp = await self._ensure()
        target = await self.resolve(path)
        try:
            data = await asyncio.to_thread(sftp.open(target, "r").read)
        except OSError as exc:
            return {"success": False, "path": path, "error": f"Read failed: {exc}"}
        text = data.decode() if isinstance(data, bytes) else data
        return {"success": True, "path": path, "content": text, "size": len(text)}

    async def write(self, path: str, content: str) -> dict[str, Any]:
        sftp = await self._ensure()
        target = await self.resolve(path)
        try:
            await asyncio.to_thread(sftp.open(target, "w").write, content)
        except OSError as exc:
            return {"success": False, "path": path, "error": f"Write failed: {exc}"}
        return {"success": True, "path": path, "size": len(content)}

    async def append(self, path: str, content: str) -> dict[str, Any]:
        sftp = await self._ensure()
        target = await self.resolve(path)
        try:
            await asyncio.to_thread(sftp.open(target, "a").write, content)
        except OSError as exc:
            return {"success": False, "path": path, "error": f"Append failed: {exc}"}
        return {"success": True, "path": path, "size": len(content)}

    async def delete(self, path: str) -> dict[str, Any]:
        sftp = await self._ensure()
        target = await self.resolve(path)
        try:
            await asyncio.to_thread(sftp.remove, target)
        except OSError as exc:
            return {"success": False, "path": path, "error": f"Delete failed: {exc}"}
        return {"success": True, "path": path}

    async def list(self, path: str) -> dict[str, Any]:
        sftp = await self._ensure()
        target = await self.resolve(path)
        try:
            entries = await asyncio.to_thread(sftp.listdir, target)
        except OSError as exc:
            return {"success": False, "path": path, "error": f"List failed: {exc}"}
        return {"success": True, "path": path, "content": "\n".join(sorted(entries)), "size": len(entries)}

    async def close(self) -> None:
        if self._sftp is not None:
            await asyncio.to_thread(self._sftp.close)
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
        self._sftp = self._client = None
