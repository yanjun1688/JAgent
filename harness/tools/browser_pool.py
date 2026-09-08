"""BrowserPool — per-Run playwright-mcp leases (ADR-011).

Trusted component. Each lease is an independent playwright-mcp stdio process
driving an independent browser instance; the Agent never selects profiles,
modes, or endpoints. Leases are lazy (first ``browser_*`` call of a Run) and
released on Run terminal events / pool shutdown / orphan cleanup.

Concurrency model:
- one asyncio.Lock per lease  → calls within one Run never fight over pages,
- one profile lock per persistent profile dir → Chrome's one-instance-per-
  profile constraint, enforced across processes via a lock file,
- different leases are different processes → parallel Runs run parallel
  browsers with no shared state.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness.core.logger import guard_logger
from harness.core.tenant import current_tenant, safe_tenant_dir_component
from harness.models.browser import BrowserConfig, BrowserMode
from harness.tools.browser_command import (
    BrowserCommandNotFoundError,
    normalize_command,
    resolve_playwright_mcp_command,
)
from harness.tools.executor import current_workspace_id

_logger = guard_logger("browser.pool")

_STALE_LOCK_AGE_SEC = 24 * 3600


class BrowserLeaseUnavailableError(RuntimeError):
    """Raised when a lease cannot be acquired (timeout / unsupported mode)."""


@dataclass
class BrowserLease:
    run_id: str
    tenant_id: str
    workspace_id: str
    mode: BrowserMode
    session: ClientSession
    tool_names: list[str]
    _call_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _transport_cm: Any = field(default=None, repr=False)
    _session_cm: Any = field(default=None, repr=False)
    _process: Any = field(default=None, repr=False)
    _profile_dir: Path | None = None
    _lock_file: Path | None = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with self._call_lock:
            return await self.session.call_tool(tool_name, arguments)

    async def close(self) -> None:
        for cm in (self._session_cm, self._transport_cm):
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except BaseException:
                    _logger.debug("lease %s cleanup failed", self.run_id, exc_info=True)
        self._release_profile_lock()

    def _release_profile_lock(self) -> None:
        if self._lock_file is not None:
            try:
                self._lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            self._lock_file = None


class BrowserPool:
    """Manages playwright-mcp leases, keyed by run_id."""

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig.from_env()
        self._leases: dict[str, BrowserLease] = {}
        self._guard = asyncio.Lock()
        self._store: Any = None

    # ── lifecycle ──────────────────────────────────────────────────

    def attach_store(self, store: Any) -> None:
        """Subscribe to terminal run events for automatic lease release."""
        self._store = store
        store.on_append(self._on_event)

    async def _on_event(self, event: Any) -> None:
        from harness.models.events import EventType

        if event.event_type in (
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_ORPHANED,
        ):
            await self.release(event.run_id)

    async def shutdown(self) -> None:
        for run_id in list(self._leases):
            await self.release(run_id)

    async def reap_stale_profile_locks(self) -> None:
        """Startup cleanup: drop profile locks older than 24h (crashed process)."""
        root = self.config.profile_root
        if not root.exists():
            return
        now = time.time()
        for lock in root.rglob("*.lock"):
            try:
                age = now - lock.stat().st_mtime
                if age > _STALE_LOCK_AGE_SEC:
                    lock.unlink(missing_ok=True)
                    _logger.info("Reaped stale profile lock %s (age %.0fh)", lock, age / 3600)
            except OSError:
                continue

    # ── lease acquisition ──────────────────────────────────────────

    async def acquire(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> BrowserLease:
        tenant_id = tenant_id or current_tenant.get() or "default"
        workspace_id = workspace_id or current_workspace_id.get() or "default"

        async with self._guard:
            existing = self._leases.get(run_id)
            if existing is not None:
                return existing

            if self.config.mode == BrowserMode.CDP:
                raise BrowserLeaseUnavailableError(
                    "cdp browser mode (remote browser farm) is not implemented yet; "
                    "use isolated or persistent mode"
                )

            profile_dir, lock_file = self._prepare_profile(tenant_id, workspace_id)
            await self._wait_profile_lock(lock_file)

            try:
                lease = await self._spawn_lease(run_id, tenant_id, workspace_id, profile_dir, lock_file)
            except BaseException:
                if lock_file is not None and lock_file.exists():
                    try:
                        lock_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise

            self._leases[run_id] = lease
            _logger.info(
                "browser lease acquired run=%s mode=%s tenant=%s ws=%s tools=%d",
                run_id,
                self.config.mode.value,
                tenant_id,
                workspace_id,
                len(lease.tool_names),
            )
            return lease

    def get(self, run_id: str) -> BrowserLease | None:
        return self._leases.get(run_id)

    async def release(self, run_id: str) -> None:
        async with self._guard:
            lease = self._leases.pop(run_id, None)
        if lease is not None:
            await lease.close()
            _logger.info("browser lease released run=%s", run_id)

    # ── spawn ──────────────────────────────────────────────────────

    def _prepare_profile(self, tenant_id: str, workspace_id: str) -> tuple[Path | None, Path | None]:
        if self.config.mode != BrowserMode.PERSISTENT:
            return None, None
        t = safe_tenant_dir_component(tenant_id)
        w = safe_tenant_dir_component(workspace_id)
        profile_dir = (self.config.profile_root / t / w).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir, profile_dir / "profile.lock"

    async def _wait_profile_lock(self, lock_file: Path | None) -> None:
        if lock_file is None:
            return
        deadline = time.monotonic() + self.config.lease_wait_ms / 1000
        while True:
            if not lock_file.exists():
                return
            if time.monotonic() >= deadline:
                raise BrowserLeaseUnavailableError(
                    f"browser profile is in use by another run (lock: {lock_file}); "
                    f"waited {self.config.lease_wait_ms}ms. Use isolated mode for parallel runs."
                )
            await asyncio.sleep(0.5)

    def _build_command(self, profile_dir: Path | None, output_dir: Path) -> list[str]:
        base = resolve_playwright_mcp_command(self.config.mcp_command)
        args: list[str] = []
        if self.config.headless:
            args.append("--headless")
        args.extend(["--output-dir", str(output_dir)])
        if self.config.mode == BrowserMode.ISOLATED:
            args.append("--isolated")
        elif self.config.mode == BrowserMode.PERSISTENT:
            assert profile_dir is not None
            args.extend(["--browser", self.config.chrome_channel, "--user-data-dir", str(profile_dir)])
        return normalize_command([*base, *args])

    async def _spawn_lease(
        self,
        run_id: str,
        tenant_id: str,
        workspace_id: str,
        profile_dir: Path | None,
        lock_file: Path | None,
    ) -> BrowserLease:
        output_dir = (self.config.output_root / safe_tenant_dir_component(tenant_id) / run_id).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        command = self._build_command(profile_dir, output_dir)

        if lock_file is not None:
            lock_file.write_text(
                json.dumps({"pid": os.getpid(), "run_id": run_id, "created_at": time.time()}),
                encoding="utf-8",
            )

        params = StdioServerParameters(command=command[0], args=command[1:])
        transport_cm = stdio_client(params)
        read, write = await asyncio.wait_for(transport_cm.__aenter__(), timeout=self.config.connect_timeout_ms / 1000)
        try:
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            try:
                await asyncio.wait_for(session.initialize(), timeout=self.config.connect_timeout_ms / 1000)
                tools_result = await asyncio.wait_for(
                    session.list_tools(), timeout=self.config.connect_timeout_ms / 1000
                )
                tool_names = [t.name for t in tools_result.tools]
                return BrowserLease(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    mode=self.config.mode,
                    session=session,
                    tool_names=tool_names,
                    _transport_cm=transport_cm,
                    _session_cm=session_cm,
                    _profile_dir=profile_dir,
                    _lock_file=lock_file,
                )
            except BaseException:
                try:
                    await session_cm.__aexit__(*sys.exc_info())
                except BaseException:
                    pass
                raise
        except BaseException:
            try:
                await transport_cm.__aexit__(*sys.exc_info())
            except BaseException:
                pass
            raise

    # ── tool discovery (for first-class registration) ──────────────

    async def discover_tools(self) -> list[Any]:
        """Spawn a short-lived probe to list playwright-mcp tools.

        Uses isolated mode so discovery never touches a persistent profile.
        Returns MCP Tool objects (name/description/inputSchema).
        """
        command = normalize_command(
            [*resolve_playwright_mcp_command(self.config.mcp_command), "--isolated", "--headless"]
        )
        params = StdioServerParameters(command=command[0], args=command[1:])
        transport_cm = stdio_client(params)
        read, write = await asyncio.wait_for(
            transport_cm.__aenter__(), timeout=self.config.connect_timeout_ms / 1000
        )
        try:
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            try:
                await asyncio.wait_for(session.initialize(), timeout=self.config.connect_timeout_ms / 1000)
                tools_result = await asyncio.wait_for(
                    session.list_tools(), timeout=self.config.connect_timeout_ms / 1000
                )
                return list(tools_result.tools)
            finally:
                try:
                    await session_cm.__aexit__(None, None, None)
                except BaseException:
                    pass
        finally:
            try:
                await transport_cm.__aexit__(None, None, None)
            except BaseException:
                pass


_pool: BrowserPool | None = None


def get_pool() -> BrowserPool | None:
    return _pool


def set_pool(pool: BrowserPool | None) -> None:
    global _pool
    _pool = pool


def browser_command_available() -> bool:
    try:
        resolve_playwright_mcp_command(None)
        return True
    except BrowserCommandNotFoundError:
        return False
