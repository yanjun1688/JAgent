"""ADR-011 §3.3 unit tests — trusted static policy for playwright-mcp tools.

Pure classification: hard-blocked / confirmation / read-only / default.
No process spawn. Cross-platform green on Linux CI.
"""

from __future__ import annotations

import pytest

from harness.models.tools import SideEffect
from harness.tools.browser_policy import (
    CONFIRMATION_TOOLS,
    EVALUATE_TOOL,
    HARD_BLOCKED_TOOLS,
    READONLY_TOOLS,
    TOOL_NAME_PREFIX,
    BrowserToolPolicy,
    is_browser_tool,
    policy_for,
    whitelist_allows,
)

# The playwright-mcp 0.0.80 surface observed via list_tools (24 tools).
BROWSER_TOOLS_24 = [
    "browser_close",
    "browser_console_messages",
    "browser_click",
    "browser_drag",
    "browser_drop",
    "browser_evaluate",
    "browser_file_upload",
    "browser_fill_form",
    "browser_find",
    "browser_handle_dialog",
    "browser_hover",
    "browser_navigate",
    "browser_navigate_back",
    "browser_network_request",
    "browser_network_requests",
    "browser_press_key",
    "browser_resize",
    "browser_run_code_unsafe",
    "browser_select_option",
    "browser_snapshot",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_type",
    "browser_wait_for",
]


class TestHardBlocked:
    @pytest.mark.parametrize("name", sorted(HARD_BLOCKED_TOOLS))
    def test_never_registered(self, name):
        policy = policy_for(name)
        assert policy.blocked is True
        assert policy.block_reason  # non-empty, explains why
        # Blocked tools must carry no accidental dangerous capabilities.
        assert policy.requires_confirmation is False
        assert policy.read_only is False

    def test_run_code_unsafe_reason_is_rce(self):
        policy = policy_for("browser_run_code_unsafe")
        assert "RCE" in policy.block_reason or "permanently disabled" in policy.block_reason


class TestEvaluateGate:
    def test_disabled_by_default(self):
        policy = policy_for(EVALUATE_TOOL)
        assert policy.blocked is True
        assert "HARNESS_BROWSER_ALLOW_EVALUATE" in policy.block_reason

    def test_allow_evaluate_still_requires_confirmation(self):
        policy = policy_for(EVALUATE_TOOL, allow_evaluate=True)
        assert policy.blocked is False
        assert policy.requires_confirmation is True

    def test_allow_evaluate_flag_does_not_unblock_unsafe(self):
        policy = policy_for("browser_run_code_unsafe", allow_evaluate=True)
        assert policy.blocked is True


class TestConfirmationTools:
    @pytest.mark.parametrize("name", sorted(CONFIRMATION_TOOLS))
    def test_requires_confirmation(self, name):
        policy = policy_for(name)
        assert policy.blocked is False
        assert policy.requires_confirmation is True
        assert policy.read_only is False
        assert policy.side_effects == [SideEffect.EXTERNAL]

    def test_file_upload_is_confirmation(self):
        assert policy_for("browser_file_upload").requires_confirmation is True


class TestReadOnlyClassification:
    @pytest.mark.parametrize("name", sorted(READONLY_TOOLS))
    def test_read_only_has_no_side_effects(self, name):
        policy = policy_for(name)
        assert policy.blocked is False
        assert policy.requires_confirmation is False
        assert policy.read_only is True
        assert policy.side_effects == []

    def test_snapshot_is_read_only(self):
        assert policy_for("browser_snapshot").side_effects == []
        assert policy_for("browser_snapshot").read_only is True

    def test_read_only_set_only_contains_browser_tools(self):
        for name in READONLY_TOOLS:
            assert name.startswith(TOOL_NAME_PREFIX)


class TestDefaultMutatingTools:
    def test_navigate_is_external_side_effect(self):
        policy = policy_for("browser_navigate")
        assert policy.blocked is False
        assert policy.read_only is False
        assert policy.requires_confirmation is False
        assert policy.side_effects == [SideEffect.EXTERNAL]

    @pytest.mark.parametrize(
        "name",
        [
            "browser_navigate",
            "browser_click",
            "browser_type",
            "browser_fill_form",
            "browser_hover",
            "browser_select_option",
            "browser_press_key",
            "browser_drag",
            "browser_drop",
            "browser_close",
            "browser_resize",
            "browser_tabs",
        ],
    )
    def test_unknown_stateful_tool_defaults_to_external(self, name):
        policy = policy_for(name)
        assert policy.blocked is False
        assert policy.side_effects == [SideEffect.EXTERNAL]


class TestPolicySideEffectsProperty:
    def test_readonly_empty_list(self):
        policy = BrowserToolPolicy(name="x", blocked=False, read_only=True)
        assert policy.side_effects == []

    def test_mutating_external(self):
        policy = BrowserToolPolicy(name="x", blocked=False, read_only=False)
        assert policy.side_effects == [SideEffect.EXTERNAL]


class TestIsBrowserTool:
    def test_prefix_match(self):
        assert is_browser_tool("browser_navigate") is True
        assert is_browser_tool("browser_run_code_unsafe") is True

    def test_non_browser_tools(self):
        for name in ("http_request", "file_op", "mcp_call", "browserish"):
            assert is_browser_tool(name) is False


class TestWhitelistPrefixMatch:
    def test_none_is_unrestricted(self):
        assert whitelist_allows(None, "browser_navigate") is True
        assert whitelist_allows(None, "http_request") is True

    def test_exact_name_match(self):
        assert whitelist_allows(["browser_navigate", "http_request"], "browser_navigate") is True

    def test_wildcard_prefix_match(self):
        assert whitelist_allows(["browser_*"], "browser_navigate") is True
        assert whitelist_allows(["browser_*"], "browser_run_code_unsafe") is True
        # Non-matching tool is not allowed by browser_*.
        assert whitelist_allows(["browser_*"], "http_request") is False

    def test_partial_prefix_glob(self):
        assert whitelist_allows(["browser_snap*"], "browser_snapshot") is True
        assert whitelist_allows(["browser_snap*"], "browser_click") is False

    def test_no_match_rejected(self):
        assert whitelist_allows(["file_op"], "browser_navigate") is False

    def test_denylist_style_absence_is_deny(self):
        # Explicit list without a browser entry (or wildcard) denies browser tools.
        assert whitelist_allows(["http_request", "file_op"], "browser_click") is False
