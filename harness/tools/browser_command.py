"""Resolve the locally-installed playwright-mcp executable (ADR-011 §3.4).

Runtime ``npx -y`` fetching is forbidden: Windows first-launch network stalls
and non-reproducible versions. The package is installed once into the project
``node_modules`` by ``scripts/setup_playwright_mcp.py``; resolution order:

    1. explicit command (HARNESS_PLAYWRIGHT_MCP_CMD / config)
    2. project node_modules/.bin/playwright-mcp[.cmd]
    3. global install on PATH
    4. BrowserCommandNotFoundError with install instructions
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from harness.core.logger import guard_logger

_logger = guard_logger("browser.command")

INSTALL_HINT = (
    "playwright-mcp is not installed. Run `python scripts/setup_playwright_mcp.py` "
    "once (installs @playwright/mcp into the project node_modules), or install it "
    "globally with `npm i -g @playwright/mcp`."
)

_SHELL_WRAP_EXES = {"npx", "npm", "node", "playwright-mcp"}


class BrowserCommandNotFoundError(RuntimeError):
    """Raised when no usable playwright-mcp executable can be found."""


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def normalize_command(command: list[str], platform: str | None = None) -> list[str]:
    """Wrap node/.cmd executables in ``cmd /c`` on Windows.

    Node-installed shims (``playwright-mcp.cmd``, ``npx.cmd``) cannot be
    spawned directly by CreateProcess; they must run via the cmd interpreter.
    Pure ``node script.js`` invocations are wrapped too (matches the
    pre-existing stdio MCP behaviour).
    """
    platform = platform or sys.platform
    if platform != "win32" or not command:
        return list(command)
    head = Path(command[0])
    exe = head.name.removesuffix(".cmd").removesuffix(".exe").removesuffix(".bat").lower()
    suffix = head.suffix.lower()
    if exe in _SHELL_WRAP_EXES or suffix in (".cmd", ".bat"):
        return ["cmd", "/c", *command]
    return list(command)


def _project_bin() -> Path | None:
    bin_name = "playwright-mcp.cmd" if sys.platform == "win32" else "playwright-mcp"
    candidate = project_root() / "node_modules" / ".bin" / bin_name
    return candidate if candidate.exists() else None


def resolve_playwright_mcp_command(explicit: list[str] | None = None) -> list[str]:
    if explicit:
        _logger.info("playwright-mcp command from explicit config: %s", explicit[0])
        return list(explicit)

    project = _project_bin()
    if project is not None:
        _logger.info("playwright-mcp command from project node_modules: %s", project)
        return [str(project)]

    global_path = shutil.which("playwright-mcp")
    if global_path:
        _logger.info("playwright-mcp command from PATH: %s", global_path)
        return [global_path]

    raise BrowserCommandNotFoundError(INSTALL_HINT)
