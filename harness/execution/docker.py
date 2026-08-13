from __future__ import annotations

import asyncio
import base64
import shlex
import shutil
from pathlib import Path
from typing import Any

from harness.execution.base import ExecutionBackend


class SandboxUnavailableError(RuntimeError):
    """Raised when the Docker carrier itself is unavailable (daemon down, CLI missing)."""


class CommandFailedError(RuntimeError):
    """Raised when a container exec command fails (e.g. file not found).

    This is a per-file-operation failure, distinct from carrier unavailability.
    File ops translate it into {"success": false, "error": ...} so the output
    contract matches LocalDirectoryBackend.
    """


class DockerSandboxBackend(ExecutionBackend):
    """Docker backend skeleton; all file access is performed inside the mount."""

    def __init__(self, image: str, host_src: str, mount: str = "/workspace") -> None:
        if shutil.which("docker") is None:
            raise SandboxUnavailableError("Docker CLI is not installed")
        self.image = image
        # Docker bind mounts require an absolute host path on Windows and
        # behave consistently across Docker Desktop and native Linux daemons.
        self.host_src = str(Path(host_src).expanduser().resolve())
        self.mount = mount
        self.container_id: str | None = None

    @property
    def root(self) -> str:
        return self.mount

    async def resolve(self, path: str) -> str:
        if not path or path.startswith("/") or ".." in path.replace("\\", "/").split("/"):
            raise PermissionError("path escapes Docker workspace mount")
        return f"{self.mount.rstrip('/')}/{path.replace(chr(92), '/')}"

    async def _ensure_container(self) -> str:
        if self.container_id:
            return self.container_id
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "-d",
            "--rm",
            "-v",
            f"{self.host_src}:{self.mount}",
            self.image,
            "sleep",
            "infinity",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise SandboxUnavailableError(err.decode(errors="replace").strip() or "docker run failed")
        self.container_id = out.decode().strip()
        return self.container_id

    async def check_available(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "info",
            "--format",
            "{{.ServerVersion}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise SandboxUnavailableError(err.decode(errors="replace").strip() or "Docker daemon is unavailable")

    async def _exec(self, *args: str, input_bytes: bytes | None = None) -> bytes:
        container = await self._ensure_container()
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            container,
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input_bytes)
        if proc.returncode != 0:
            raise CommandFailedError(err.decode(errors="replace").strip() or "docker exec failed")
        return out

    async def read(self, path: str) -> dict[str, Any]:
        target = await self.resolve(path)
        try:
            content = await self._exec("cat", target)
        except CommandFailedError as exc:
            return {"success": False, "path": path, "error": f"Read failed: {exc}"}
        text = content.decode("utf-8")
        return {"success": True, "path": path, "content": text, "size": len(text)}

    async def write(self, path: str, content: str) -> dict[str, Any]:
        target = await self.resolve(path)
        encoded = base64.b64encode(content.encode()).decode()
        quoted = shlex.quote(target)
        command = "mkdir -p " + shlex.quote(target.rsplit("/", 1)[0]) + f" && echo {encoded} | base64 -d > {quoted}"
        try:
            await self._exec("sh", "-c", command)
        except CommandFailedError as exc:
            return {"success": False, "path": path, "error": f"Write failed: {exc}"}
        return {"success": True, "path": path, "size": len(content)}

    async def append(self, path: str, content: str) -> dict[str, Any]:
        target = await self.resolve(path)
        encoded = base64.b64encode(content.encode()).decode()
        quoted = shlex.quote(target)
        command = "mkdir -p " + shlex.quote(target.rsplit("/", 1)[0]) + f" && echo {encoded} | base64 -d >> {quoted}"
        try:
            await self._exec("sh", "-c", command)
        except CommandFailedError as exc:
            return {"success": False, "path": path, "error": f"Append failed: {exc}"}
        return {"success": True, "path": path, "size": len(content)}

    async def delete(self, path: str) -> dict[str, Any]:
        target = await self.resolve(path)
        try:
            await self._exec("rm", "-f", target)
        except CommandFailedError as exc:
            return {"success": False, "path": path, "error": f"Delete failed: {exc}"}
        return {"success": True, "path": path}

    async def list(self, path: str) -> dict[str, Any]:
        target = await self.resolve(path)
        try:
            output = await self._exec("find", target, "-mindepth", "1", "-maxdepth", "1", "-printf", "%f\n")
        except CommandFailedError as exc:
            return {"success": False, "path": path, "error": f"List failed: {exc}"}
        entries = output.decode().splitlines()
        return {"success": True, "path": path, "content": "\n".join(sorted(entries)), "size": len(entries)}

    async def close(self) -> None:
        if self.container_id:
            proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", self.container_id)
            await proc.wait()
            self.container_id = None
