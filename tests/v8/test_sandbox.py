"""G-06 · `shell.exec` 命令体沙箱。

纯函数用例逐条覆盖 ``sandbox.check_command`` 的 7 条规则；端到端用例证明
registry 在**启动进程之前**就拒绝，并且外部文件内容不会出现在 stdout 里；
``scrub_env`` 单独验证 home/tmp 被关进 workspace。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jagent.harness.registry import build_default_registry
from jagent.harness.sandbox import SandboxVerdict, check_command, scrub_env

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义，G-06）；单用户本地 Harness 是 Windows",
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def secret(tmp_path: Path) -> Path:
    """workspace 之外的一个「机密」文件；端到端用例断言它没被读出来。"""
    path = tmp_path / "TOPSECRET.txt"
    path.write_text("TOPSECRET", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# check_command · 放行
# --------------------------------------------------------------------------- #

def test_allows_whitelisted_commands(workspace: Path) -> None:
    """正向对照：白名单里的解释器与 git 不能被误伤。"""
    for cmd in ("python -m pytest tests -q", "git status", "echo hi", "where python"):
        verdict = check_command(cmd, workspace)
        assert isinstance(verdict, SandboxVerdict)
        assert verdict.ok is True, f"{cmd!r} should be allowed: {verdict.reason}"


def test_allows_parent_segment_that_stays_inside(workspace: Path) -> None:
    """a/../b 解析后仍在 workspace 内，属于合法路径。"""
    verdict = check_command(r"type a/../b.txt", workspace)

    assert verdict.ok is True, verdict.reason


def test_allows_parent_segment_relative_to_cwd(workspace: Path) -> None:
    """cwd 在子目录时，``..`` 回到 workspace 根是合法的（不能按根目录误判）。"""
    sub = workspace / "sub"
    sub.mkdir()

    verdict = check_command(r"type ..\notes.txt", workspace, cwd=sub)

    assert verdict.ok is True, verdict.reason


def test_allows_absolute_interpreter_path(workspace: Path) -> None:
    """Windows 上 sys.executable 是绝对路径；解释器位置放行，路径参数仍受限。"""
    verdict = check_command(f'"{sys.executable}" -c "print(1)"', workspace)

    assert verdict.ok is True, verdict.reason


def test_allows_redirect_inside_workspace(workspace: Path) -> None:
    verdict = check_command("echo hi > out.txt", workspace)

    assert verdict.ok is True, verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 1：绝对路径
# --------------------------------------------------------------------------- #

def test_denies_windows_absolute_path(workspace: Path) -> None:
    verdict = check_command(r"type C:\Windows\win.ini", workspace)

    assert verdict.ok is False
    assert "workspace" in verdict.reason
    assert "C:\\Windows" in verdict.detail


def test_denies_posix_absolute_path(workspace: Path) -> None:
    verdict = check_command("type /etc/passwd", workspace)

    assert verdict.ok is False
    assert "workspace" in verdict.reason


def test_denies_unc_path(workspace: Path) -> None:
    verdict = check_command(r"type \\server\share\secret.txt", workspace)

    assert verdict.ok is False
    assert "UNC" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 2：父目录逃逸
# --------------------------------------------------------------------------- #

def test_denies_parent_traversal_outside_workspace(workspace: Path) -> None:
    verdict = check_command(r"type ..\..\TOPSECRET.txt", workspace)

    assert verdict.ok is False
    assert "父目录逃逸" in verdict.reason
    assert "resolved=" in verdict.detail


def test_denies_caret_hidden_parent_traversal(workspace: Path) -> None:
    """``^..`` 在 cmd.exe 里等价于 ``..``，脱字符不能成为绕过手段。"""
    verdict = check_command(r"type ^..\^..\TOPSECRET.txt", workspace)

    assert verdict.ok is False
    assert "父目录逃逸" in verdict.reason


def test_denies_newline_separated_command(workspace: Path) -> None:
    verdict = check_command("echo hi\nformat C:", workspace)

    assert verdict.ok is False
    assert "白名单" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 4：环境变量展开
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "cmd",
    [
        r"type %USERPROFILE%\.ssh\id_rsa",
        "type $HOME/.ssh/id_rsa",
        r"type $env:USERPROFILE\secret.txt",
    ],
)
def test_denies_environment_variable_expansion(workspace: Path, cmd: str) -> None:
    verdict = check_command(cmd, workspace)

    assert verdict.ok is False
    assert "环境变量" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 3：符号链接逃逸
# --------------------------------------------------------------------------- #

def test_denies_symlink_escape(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 取决于 Windows 权限
        pytest.skip("当前环境不允许创建符号链接（Windows 需要开发者模式或管理员）")

    verdict = check_command(r"type link\secret.txt", workspace)

    assert verdict.ok is False
    assert "workspace" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 5：重定向目标
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("operator", [">", ">>"])
def test_denies_redirect_outside_workspace(workspace: Path, operator: str) -> None:
    verdict = check_command(rf"echo hi {operator} ..\out.txt", workspace)

    assert verdict.ok is False
    assert "重定向" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 6：命令白名单
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "cmd",
    [
        "del important.txt",
        "format C:",
        "reg query HKLM",
        "net user",
        "powershell -Command Remove-Item x",
        "cmd /c echo hi",
        "cmd.exe /c type ..\\..\\TOPSECRET.txt",
    ],
)
def test_denies_non_whitelisted_binary(workspace: Path, cmd: str) -> None:
    verdict = check_command(cmd, workspace)

    assert verdict.ok is False
    assert "白名单" in verdict.reason


def test_denies_chained_second_command(workspace: Path) -> None:
    """复合命令的每一段都要过白名单，不能只看第一个 token。"""
    for cmd in ("echo hi && del x", "git status; format C:", "type a.txt | powershell -c x"):
        verdict = check_command(cmd, workspace)
        assert verdict.ok is False, f"{cmd!r} should be denied"
        assert "白名单" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 边界
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd", ["", "   ", "> out.txt", "&&"])
def test_denies_empty_command(workspace: Path, cmd: str) -> None:
    verdict = check_command(cmd, workspace)

    assert verdict.ok is False


# --------------------------------------------------------------------------- #
# 端到端：registry 真的不启动进程
# --------------------------------------------------------------------------- #

def test_shell_exec_denies_traversal_and_does_not_leak(
    workspace: Path, secret: Path
) -> None:
    registry = build_default_registry(workspace)

    result = registry.call(
        "shell.exec", {"cmd": r"type ..\..\TOPSECRET.txt"}, workspace=workspace
    )

    assert result.ok is False
    assert result.error_code == "sandbox_denied"
    assert result.stdout == ""
    assert "TOPSECRET" not in (result.stdout or "")
    assert result.data == {}
    assert secret.read_text(encoding="utf-8") == "TOPSECRET"  # 文件本身没被动过


def test_shell_exec_denied_before_process_starts(workspace: Path, secret: Path) -> None:
    """拒绝发生在启动之前：重定向连空文件都不该被创建。"""
    registry = build_default_registry(workspace)
    marker = workspace / "marker.txt"

    result = registry.call(
        "shell.exec",
        {"cmd": r"type ..\..\TOPSECRET.txt > marker.txt"},
        workspace=workspace,
    )

    assert result.ok is False
    assert result.error_code == "sandbox_denied"
    assert not marker.exists(), "沙箱拒绝之后不能有任何进程被启动"
    assert not (workspace / ".sandbox").exists(), "拒绝时连 scrub_env 都不该跑"


def test_shell_exec_runs_inline_python(workspace: Path) -> None:
    """正向对照：正常命令仍然能跑，stdout 照旧是观测。"""
    registry = build_default_registry(workspace)

    result = registry.call(
        "shell.exec", {"cmd": 'python -c "print(1)"'}, workspace=workspace
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout.strip() == "1"


# --------------------------------------------------------------------------- #
# scrub_env
# --------------------------------------------------------------------------- #

def test_scrub_env_redirects_home_and_tmp_into_workspace(workspace: Path) -> None:
    env = scrub_env({"PATH": "keep-me", "HOME": "C:\\Users\\someone"}, workspace)

    sandbox = workspace / ".sandbox"
    assert env["HOME"] == str(sandbox / "home")
    assert env["USERPROFILE"] == str(sandbox / "home")
    assert env["TEMP"] == str(sandbox / "tmp")
    assert env["TMP"] == str(sandbox / "tmp")
    assert (sandbox / "home").is_dir()
    assert (sandbox / "tmp").is_dir()
    # PATH 原有条目保留，但服务解释器排第一（契约见 test_shell_interpreter.py）。
    assert env["PATH"].endswith(os.pathsep + "keep-me")
    assert Path(env["PATH"].split(os.pathsep)[0]).resolve() == Path(sys.executable).resolve().parent


def test_scrub_env_does_not_mutate_base(workspace: Path) -> None:
    base = {"HOME": "C:\\Users\\someone", "PATH": "keep-me"}

    env = scrub_env(base, workspace)

    assert base == {"HOME": "C:\\Users\\someone", "PATH": "keep-me"}
    assert env is not base
    assert env["HOME"] != base["HOME"]
