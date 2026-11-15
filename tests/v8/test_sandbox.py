"""G-06 v2 · `shell.exec` 命令体沙箱（POSIX 统一底座，D-037）。

纯函数用例逐条覆盖 ``sandbox.check_command`` 的规则；端到端用例证明
registry 在**启动进程之前**就拒绝，并且外部文件内容不会出现在 stdout 里；
``scrub_env`` 单独验证 home/tmp 被关进 workspace、MSYS 路径改写被关闭。

两平台同一套语义：命令体经 `bash -c` 执行（Windows 用 Git for Windows
自带的 bash），规则集是 POSIX 的——见 docs/jagent-v8-hardening.md §5。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jagent.harness.registry import build_default_registry
from jagent.harness.sandbox import SandboxVerdict, check_command, scrub_env


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
    """正向对照：白名单里的解释器、git 与 POSIX 文件命令不能被误伤。"""
    for cmd in (
        "python -m pytest tests -q",
        "git status",
        "echo hi",
        "which python",
        "cat notes/a.txt",
        "ls -la",
        "grep -rn TODO .",
    ):
        verdict = check_command(cmd, workspace)
        assert isinstance(verdict, SandboxVerdict)
        assert verdict.ok is True, f"{cmd!r} should be allowed: {verdict.reason}"


def test_allows_parent_segment_that_stays_inside(workspace: Path) -> None:
    """a/../b 解析后仍在 workspace 内，属于合法路径。"""
    verdict = check_command("cat a/../b.txt", workspace)

    assert verdict.ok is True, verdict.reason


def test_allows_parent_segment_relative_to_cwd(workspace: Path) -> None:
    """cwd 在子目录时，``..`` 回到 workspace 根是合法的（不能按根目录误判）。"""
    sub = workspace / "sub"
    sub.mkdir()

    verdict = check_command("cat ../notes.txt", workspace, cwd=sub)

    assert verdict.ok is True, verdict.reason


def test_allows_absolute_interpreter_path(workspace: Path) -> None:
    """sys.executable 是绝对路径；解释器位置放行，路径参数仍受限。"""
    verdict = check_command(f'"{sys.executable}" -c "print(1)"', workspace)

    assert verdict.ok is True, verdict.reason


def test_allows_redirect_inside_workspace(workspace: Path) -> None:
    verdict = check_command("echo hi > out.txt", workspace)

    assert verdict.ok is True, verdict.reason


def test_allows_caret_as_literal(workspace: Path) -> None:
    """POSIX 里 ``^`` 不是转义符：脱字符是普通字面量，不再有 cmd 的隐藏语义。"""
    verdict = check_command("echo ^hi", workspace)

    assert verdict.ok is True, verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 1：绝对路径
# --------------------------------------------------------------------------- #

def test_denies_drive_absolute_path(workspace: Path) -> None:
    verdict = check_command("cat C:/Windows/win.ini", workspace)

    assert verdict.ok is False
    assert "workspace" in verdict.reason
    assert "C:" in verdict.detail


def test_denies_windows_backslash_absolute_path(workspace: Path) -> None:
    """Windows 风格拼写（盘符 + 反斜杠）在 POSIX 底座上同样拒绝：反斜杠
    在引号外是转义符，宁多拒；盘符单独由规则 1 拦。"""
    verdict = check_command("cat 'C:\\Windows\\win.ini'", workspace)

    assert verdict.ok is False


def test_denies_posix_absolute_path(workspace: Path) -> None:
    verdict = check_command("cat /etc/passwd", workspace)

    assert verdict.ok is False
    assert "workspace" in verdict.reason


@pytest.mark.parametrize("token", ["/etc", "/tmp", "/usr", "/x"])
def test_denies_posix_root_fragments(workspace: Path, token: str) -> None:
    """``/I`` 这类 Windows 短开关特判已删除：``/x`` 就是 POSIX 绝对路径。"""
    verdict = check_command(f"cat {token}", workspace)

    assert verdict.ok is False


def test_denies_unc_path(workspace: Path) -> None:
    verdict = check_command("cat //server/share/secret.txt", workspace)

    assert verdict.ok is False
    assert "UNC" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 2：父目录逃逸
# --------------------------------------------------------------------------- #

def test_denies_parent_traversal_outside_workspace(workspace: Path) -> None:
    verdict = check_command("cat ../../TOPSECRET.txt", workspace)

    assert verdict.ok is False
    assert "父目录逃逸" in verdict.reason
    assert "resolved=" in verdict.detail


def test_denies_newline_separated_command(workspace: Path) -> None:
    verdict = check_command("echo hi\nformat C:", workspace)

    assert verdict.ok is False
    assert "白名单" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 2b：反斜杠在引号外是转义符（宁多拒）
# --------------------------------------------------------------------------- #

def test_denies_backslash_outside_quotes(workspace: Path) -> None:
    """POSIX bash 里引号外的反斜杠是转义符：``\\..\\`` 能把父目录逃逸藏过去，
    静态检查不可靠解析——引号外出现反斜杠一律拒绝（宁多拒）。"""
    for cmd in ("cat ..\\secret.txt", "echo a\\b", "git log --pretty=\\n"):
        verdict = check_command(cmd, workspace)
        assert verdict.ok is False, f"{cmd!r} should be denied: {verdict.reason}"
        assert "反斜杠" in verdict.reason


def test_allows_backslash_inside_quotes(workspace: Path) -> None:
    """引号内反斜杠是字面量（bash 双引号不展开 ``\\n`` 这类），正常放行。"""
    verdict = check_command("python -c \"print('a\\\\nb')\"", workspace)

    assert verdict.ok is True, verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 4：环境变量展开（含 tilde）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "cmd",
    [
        "cat $HOME/.ssh/id_rsa",
        "cat ${HOME}/.ssh/id_rsa",
        "echo $(whoami)",
        "cat $env:USERPROFILE/secret.txt",
        "cat %USERPROFILE%/.ssh/id_rsa",
        "cat ~/.ssh/id_rsa",
        "ls ~",
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
    except (OSError, NotImplementedError):  # pragma: no cover - 取决于平台权限
        pytest.skip("当前环境不允许创建符号链接")

    verdict = check_command("cat link/secret.txt", workspace)

    assert verdict.ok is False
    assert "workspace" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 5：重定向目标
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("operator", [">", ">>"])
def test_denies_redirect_outside_workspace(workspace: Path, operator: str) -> None:
    verdict = check_command(f"echo hi {operator} ../out.txt", workspace)

    assert verdict.ok is False
    assert "重定向" in verdict.reason


# --------------------------------------------------------------------------- #
# check_command · 规则 6：命令白名单
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf x",
        "format C:",
        "reg query HKLM",
        "net user",
        "powershell -Command Remove-Item x",
        "sh -c echo hi",
        "bash -c cat ../../TOPSECRET.txt",
        # v1 的 cmd 内建命令在 POSIX 底座上不再是白名单成员：
        "type a.txt",
        "dir",
        "where python",
        "findstr TODO notes.txt",
    ],
)
def test_denies_non_whitelisted_binary(workspace: Path, cmd: str) -> None:
    verdict = check_command(cmd, workspace)

    assert verdict.ok is False
    assert "白名单" in verdict.reason


def test_denies_chained_second_command(workspace: Path) -> None:
    """复合命令的每一段都要过白名单，不能只看第一个 token。"""
    for cmd in ("echo hi && rm x", "git status; format C:", "cat a.txt | powershell -c x"):
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
        "shell.exec", {"cmd": "cat ../../TOPSECRET.txt"}, workspace=workspace
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
        {"cmd": "cat ../../TOPSECRET.txt > marker.txt"},
        workspace=workspace,
    )

    assert result.ok is False
    assert result.error_code == "sandbox_denied"
    assert not marker.exists(), "沙箱拒绝之后不能有任何进程被启动"
    assert not (workspace / ".sandbox").exists(), "拒绝时连 scrub_env 都不该跑"


def test_shell_exec_runs_inline_python(workspace: Path) -> None:
    """正向对照：正常命令经 bash 跑，stdout 照旧是观测。"""
    registry = build_default_registry(workspace)

    result = registry.call(
        "shell.exec", {"cmd": 'python -c "print(1)"'}, workspace=workspace
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout.strip() == "1"


def test_shell_exec_executes_via_bash(workspace: Path, monkeypatch) -> None:
    """执行入口是 ``bash -c``：argv 形状断言（D-037 底座统一的锚点）。"""
    captured: dict = {}

    class FakeProc:
        def __init__(self) -> None:
            self.stdout = "ok"
            self.stderr = ""
            self.returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("jagent.harness.registry.subprocess.run", fake_run)
    registry = build_default_registry(workspace)

    registry.call("shell.exec", {"cmd": "echo hi"}, workspace=workspace)

    assert Path(captured["argv"][0]).name.lower().startswith("bash")
    assert captured["argv"][1:2] == ["-c"]
    assert captured["argv"][2] == "echo hi"
    assert captured["kwargs"]["encoding"] == "utf-8"


# --------------------------------------------------------------------------- #
# scrub_env（含 MSYS 路径改写关闭）
# --------------------------------------------------------------------------- #

def test_scrub_env_redirects_home_and_tmp_into_workspace(workspace: Path) -> None:
    env = scrub_env({"PATH": "keep-me", "HOME": str(Path.home())}, workspace)

    sandbox = workspace / ".sandbox"
    assert env["HOME"] == str(sandbox / "home")
    assert env["USERPROFILE"] == str(sandbox / "home")
    assert env["TEMP"] == str(sandbox / "tmp")
    assert env["TMP"] == str(sandbox / "tmp")
    assert (sandbox / "home").is_dir()
    assert (sandbox / "tmp").is_dir()
    # PATH 原有条目保留，但服务解释器排第一（未解析符号链接的目录，契约见
    # test_shell_interpreter.py / D-038）。
    assert env["PATH"].endswith(os.pathsep + "keep-me")
    assert Path(env["PATH"].split(os.pathsep)[0]) == Path(sys.executable).parent


def test_scrub_env_disables_msys_path_conversion(workspace: Path) -> None:
    """Windows 上 Git Bash 调用原生 exe 会改写 POSIX 风格参数（/etc → Git 安装目录）：
    注入 MSYS2_ARG_CONV_EXCL 关掉改写，放行命令的参数不被静默变形。"""
    env = scrub_env({"PATH": "keep-me"}, workspace)

    if sys.platform == "win32":
        assert env["MSYS2_ARG_CONV_EXCL"] == "*"
    else:
        assert "MSYS2_ARG_CONV_EXCL" not in env  # POSIX 上无需也不注入


def test_scrub_env_does_not_mutate_base(workspace: Path) -> None:
    base = {"HOME": str(Path.home()), "PATH": "keep-me"}

    env = scrub_env(base, workspace)

    assert base == {"HOME": str(Path.home()), "PATH": "keep-me"}
    assert env is not base
    assert env["HOME"] != base["HOME"]
