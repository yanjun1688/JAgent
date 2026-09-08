#!/usr/bin/env python
"""Install and verify the local playwright-mcp backend (ADR-011 §3.4).

Runtime ``npx -y`` fetching is forbidden in production runs. This script
installs the version pinned in package.json into the project ``node_modules``
once, then verifies the executable works:

    python scripts/setup_playwright_mcp.py

Exit code 0 = ready; non-zero = install/verification failed (message tells
the operator what to do).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BIN_NAME = "playwright-mcp.cmd" if sys.platform == "win32" else "playwright-mcp"


def _run(cmd: list[str]) -> int:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print("ERROR: npm not found on PATH. Install Node.js 18+ (https://nodejs.org/) and retry.")
        return 1

    print("[1/3] Installing pinned @playwright/mcp into project node_modules ...")
    rc = _run([npm, "install", "--no-audit", "--no-fund"])
    if rc != 0:
        print("ERROR: npm install failed. Check network/npm registry access and retry.")
        return rc

    bin_path = ROOT / "node_modules" / ".bin" / _BIN_NAME
    if not bin_path.exists():
        print(f"ERROR: expected executable not found after install: {bin_path}")
        return 1

    print(f"[2/3] Executable present: {bin_path}")

    print("[3/3] Verifying playwright-mcp starts and lists browser tools ...")
    try:
        ok = asyncio.run(_verify())
    except Exception as exc:  # noqa: BLE001 — operator-facing script
        print(f"ERROR: verification failed: {exc}")
        return 1
    if not ok:
        print("ERROR: playwright-mcp started but exposed no browser tools (unexpected).")
        return 1

    print("\nOK: playwright-mcp is installed and verified.")
    print("    Browser tools register automatically on server start (see ADR-011).")
    return 0


async def _verify() -> bool:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from harness.tools.browser_command import normalize_command, resolve_playwright_mcp_command

    command = normalize_command([*resolve_playwright_mcp_command(None), "--isolated", "--headless"])
    params = StdioServerParameters(command=command[0], args=command[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=60)
            tools = await asyncio.wait_for(session.list_tools(), timeout=60)
    names = [t.name for t in tools.tools]
    browser_names = [n for n in names if n.startswith("browser_")]
    print(f"    discovered {len(browser_names)} browser tools (unsafe ones are filtered at registration)")
    return bool(browser_names)


if __name__ == "__main__":
    sys.exit(main())
