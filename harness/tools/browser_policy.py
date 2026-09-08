"""Trusted static policy for playwright-mcp tools (ADR-011 §3.3).

playwright-mcp is explicitly NOT a security boundary; the harness trusted
layer is. Every discovered MCP tool is passed through this policy before
registration:

- hard-blocked tools are never registered (invisible / uncallable),
- tools may be marked read-only, confirmation-required, or state-mutating,
- all browser tools are serialized per lease and never idempotency-cached.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.models.tools import SideEffect

TOOL_NAME_PREFIX = "browser_"

READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "browser_snapshot",
        "browser_find",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_wait_for",
    }
)

CONFIRMATION_TOOLS: frozenset[str] = frozenset({"browser_file_upload"})

HARD_BLOCKED_TOOLS: frozenset[str] = frozenset({"browser_run_code_unsafe"})

EVALUATE_TOOL = "browser_evaluate"


@dataclass(frozen=True)
class BrowserToolPolicy:
    name: str
    blocked: bool
    block_reason: str = ""
    requires_confirmation: bool = False
    read_only: bool = False

    @property
    def side_effects(self) -> list[SideEffect]:
        return [] if self.read_only else [SideEffect.EXTERNAL]


def policy_for(tool_name: str, *, allow_evaluate: bool = False) -> BrowserToolPolicy:
    if tool_name in HARD_BLOCKED_TOOLS:
        return BrowserToolPolicy(
            name=tool_name,
            blocked=True,
            block_reason="RCE-class tool is permanently disabled by harness security policy",
        )
    if tool_name == EVALUATE_TOOL and not allow_evaluate:
        return BrowserToolPolicy(
            name=tool_name,
            blocked=True,
            block_reason=(
                "browser_evaluate executes arbitrary JavaScript; disabled by default "
                "(set HARNESS_BROWSER_ALLOW_EVALUATE=1 to enable with human confirmation)"
            ),
        )
    if tool_name == EVALUATE_TOOL:
        return BrowserToolPolicy(name=tool_name, blocked=False, requires_confirmation=True)
    if tool_name in CONFIRMATION_TOOLS:
        return BrowserToolPolicy(name=tool_name, blocked=False, requires_confirmation=True)
    if tool_name in READONLY_TOOLS:
        return BrowserToolPolicy(name=tool_name, blocked=False, read_only=True)
    return BrowserToolPolicy(name=tool_name, blocked=False)


def is_browser_tool(tool_name: str) -> bool:
    return tool_name.startswith(TOOL_NAME_PREFIX)


def whitelist_allows(allowed_tools: list[str] | None, tool_name: str) -> bool:
    """Workspace allowed_tools check with ``browser_*`` prefix support.

    ``None`` means unrestricted (legacy behaviour); an explicit list must
    contain the exact tool name or a ``prefix*`` glob (e.g. ``browser_*``).
    """
    if allowed_tools is None:
        return True
    if tool_name in allowed_tools:
        return True
    for entry in allowed_tools:
        if entry.endswith("*") and tool_name.startswith(entry[:-1]):
            return True
    return False
