"""Browser pool configuration (ADR-011) — trusted, env-sourced.

The Agent never supplies profile paths, mode, or browser endpoints; all
browser topology is decided by this trusted configuration object.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class BrowserMode(str, Enum):
    ISOLATED = "isolated"
    PERSISTENT = "persistent"
    CDP = "cdp"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class BrowserConfig(BaseModel):
    mode: BrowserMode = BrowserMode.ISOLATED
    profile_root: Path = Path("data/browser-profiles")
    output_root: Path = Path("data/playwright-mcp")
    headless: bool = False
    chrome_channel: str = "chrome"
    allow_evaluate: bool = False
    lease_wait_ms: int = Field(default=30000, ge=0)
    cdp_endpoint: str | None = None
    mcp_command: list[str] | None = None
    connect_timeout_ms: int = Field(default=60000, ge=1000)

    @classmethod
    def from_env(cls) -> "BrowserConfig":
        mode = BrowserMode(os.environ.get("HARNESS_BROWSER_MODE", "isolated").strip().lower())
        return cls(
            mode=mode,
            profile_root=Path(os.environ.get("HARNESS_BROWSER_PROFILE_ROOT", "data/browser-profiles")),
            output_root=Path(os.environ.get("HARNESS_BROWSER_OUTPUT_ROOT", "data/playwright-mcp")),
            headless=_env_bool("HARNESS_BROWSER_HEADLESS", False),
            chrome_channel=os.environ.get("HARNESS_BROWSER_CHROME_CHANNEL", "chrome"),
            allow_evaluate=_env_bool("HARNESS_BROWSER_ALLOW_EVALUATE", False),
            lease_wait_ms=int(os.environ.get("HARNESS_BROWSER_LEASE_WAIT_MS", "30000")),
            cdp_endpoint=os.environ.get("HARNESS_BROWSER_CDP_ENDPOINT") or None,
            mcp_command=_split_cmd(os.environ.get("HARNESS_PLAYWRIGHT_MCP_CMD")),
        )


def _split_cmd(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    import shlex

    return shlex.split(raw, posix=os.name != "nt")
