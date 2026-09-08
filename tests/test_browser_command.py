"""ADR-011 §3.4 unit tests — playwright-mcp command resolution + normalization.

Pure functions, no process spawn, cross-platform green on Linux CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness.models.browser import BrowserConfig
from harness.tools.browser_command import (
    INSTALL_HINT,
    BrowserCommandNotFoundError,
    normalize_command,
    resolve_playwright_mcp_command,
)


class TestNormalizeCommand:
    """Windows cmd /c wrapping for node shims (shared with MCP stdio path)."""

    def test_windows_wraps_npx(self):
        assert normalize_command(["npx", "-y", "@playwright/mcp"], "win32") == [
            "cmd",
            "/c",
            "npx",
            "-y",
            "@playwright/mcp",
        ]

    def test_windows_wraps_npm(self):
        assert normalize_command(["npm", "run", "start"], "win32") == ["cmd", "/c", "npm", "run", "start"]

    def test_windows_wraps_node(self):
        assert normalize_command(["node", "server.js"], "win32") == ["cmd", "/c", "node", "server.js"]

    def test_windows_wraps_playwright_mcp_bare(self):
        assert normalize_command(["playwright-mcp", "--isolated"], "win32") == [
            "cmd",
            "/c",
            "playwright-mcp",
            "--isolated",
        ]

    def test_windows_wraps_cmd_suffix_path(self):
        assert normalize_command([r"C:\proj\node_modules\.bin\playwright-mcp.cmd", "--browser", "chrome"], "win32") == [
            "cmd",
            "/c",
            r"C:\proj\node_modules\.bin\playwright-mcp.cmd",
            "--browser",
            "chrome",
        ]

    def test_windows_wraps_bat_suffix(self):
        assert normalize_command([r"C:\bin\tool.bat", "arg"], "win32") == ["cmd", "/c", r"C:\bin\tool.bat", "arg"]

    def test_windows_does_not_wrap_python(self):
        assert normalize_command(["python", "script.py"], "win32") == ["python", "script.py"]

    def test_windows_does_not_wrap_plain_exe_path(self):
        assert normalize_command([r"C:\Tools\custom-runner.exe", "a"], "win32") == [r"C:\Tools\custom-runner.exe", "a"]

    def test_windows_empty_command_unchanged(self):
        assert normalize_command([], "win32") == []

    def test_non_windows_never_wraps(self):
        for platform in ("linux", "darwin"):
            assert normalize_command(["npx", "-y", "@playwright/mcp"], platform) == ["npx", "-y", "@playwright/mcp"]
            assert normalize_command(["playwright-mcp", "--isolated"], platform) == ["playwright-mcp", "--isolated"]
            assert normalize_command([r"C:\bin\playwright-mcp.cmd"], platform) == [r"C:\bin\playwright-mcp.cmd"]

    def test_default_platform_uses_current(self):
        # Deterministic wrapper: playwright-mcp is always wrapped on win32 only.
        cmd = ["playwright-mcp", "--isolated"]
        result = normalize_command(cmd)
        if sys.platform == "win32":
            assert result == ["cmd", "/c", "playwright-mcp", "--isolated"]
        else:
            assert result == cmd


class TestResolveCommand:
    """Resolution order: explicit → project node_modules → PATH → error."""

    def test_explicit_command_wins(self, monkeypatch):
        # Even when project bin exists, an explicit command must win (ADR-011).
        monkeypatch.setattr("harness.tools.browser_command._project_bin", lambda: Path("/proj/bin/playwright-mcp.cmd"))
        assert resolve_playwright_mcp_command(["override-cmd", "--x"]) == ["override-cmd", "--x"]

    def test_project_node_modules_before_global(self, monkeypatch):
        project_bin = Path("/proj/node_modules/.bin/playwright-mcp.cmd")
        monkeypatch.setattr("harness.tools.browser_command._project_bin", lambda: project_bin)
        monkeypatch.setattr("harness.tools.browser_command.shutil.which", lambda _: "/usr/local/bin/playwright-mcp")
        assert resolve_playwright_mcp_command(None) == [str(project_bin)]

    def test_global_path_fallback(self, monkeypatch):
        monkeypatch.setattr("harness.tools.browser_command._project_bin", lambda: None)
        monkeypatch.setattr("harness.tools.browser_command.shutil.which", lambda _: "/usr/local/bin/playwright-mcp")
        assert resolve_playwright_mcp_command(None) == ["/usr/local/bin/playwright-mcp"]

    def test_missing_raises_with_install_hint(self, monkeypatch):
        monkeypatch.setattr("harness.tools.browser_command._project_bin", lambda: None)
        monkeypatch.setattr("harness.tools.browser_command.shutil.which", lambda _: None)
        with pytest.raises(BrowserCommandNotFoundError) as exc:
            resolve_playwright_mcp_command(None)
        assert INSTALL_HINT in str(exc.value)

    def test_explicit_empty_does_not_bypass_resolution(self, monkeypatch):
        # explicit=None (default) → resolve from project/global as normal.
        project_bin = Path("/proj/node_modules/.bin/playwright-mcp")
        monkeypatch.setattr("harness.tools.browser_command._project_bin", lambda: project_bin)
        assert resolve_playwright_mcp_command() == [str(project_bin)]


class TestEnvCommandConfig:
    def test_env_override_is_parsed_into_config(self, monkeypatch):
        monkeypatch.setenv("HARNESS_PLAYWRIGHT_MCP_CMD", "my-pw-cmd --isolated")
        cfg = BrowserConfig.from_env()
        assert cfg.mcp_command == ["my-pw-cmd", "--isolated"]

    def test_env_empty_means_no_explicit_override(self, monkeypatch):
        monkeypatch.delenv("HARNESS_PLAYWRIGHT_MCP_CMD", raising=False)
        cfg = BrowserConfig.from_env()
        assert cfg.mcp_command is None
