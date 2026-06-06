"""Sandbox execution — unified tool execution entry with timeout control."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


class Sandbox:
    @staticmethod
    async def invoke(
        tool_fn: Callable[[dict[str, Any]], Any],
        input: dict[str, Any],
        *,
        timeout_ms: int = 30000,
    ) -> Any:
        """Execute a tool function with timeout control.

        NOTE: This is a PUBLIC method accessible to all components.
        Trusted callers should route through ToolExecutor.execute() to get
        guardrails, idempotency, event recording, and confirmation support.
        Direct use of Sandbox.invoke() bypasses all trust boundary protections.
        """
        call = tool_fn(input)
        if asyncio.iscoroutine(call):
            return await asyncio.wait_for(call, timeout=timeout_ms / 1000.0)
        return call

    @staticmethod
    async def run(
        command: list[str],
        *,
        timeout_ms: int = 30000,
        cwd: str | None = None,
        # TODO(L5): 实现进程路径白名单限制
        _allow_paths: list[str] | None = None,
    ) -> SandboxResult:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_ms / 1000.0,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            return SandboxResult(
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                exit_code=proc.returncode if proc.returncode is not None else 0,
                duration_ms=duration_ms,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration_ms = int((time.monotonic() - start) * 1000)
            raise
