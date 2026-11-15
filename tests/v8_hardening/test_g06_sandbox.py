r"""G-06 shell 沙箱 · 独立验收。

需求（来自用户与审计 G-06）：
1. shell.exec 的命令体不能读写 workspace 外的文件：`..\..\x`、`C:\Windows\...`、
   `\\server\share`、`%USERPROFILE%\x`、`$HOME/x` 全部 sandbox_denied，
   且 stdout 里不能出现外部文件内容。
2. 重定向到外部（`> ..\x`）也要拒。
3. 合法命令要能跑：python -c "print(1)"、pytest --version、git status。
4. scrub_env 把 HOME/TEMP 指到 workspace 内（子进程 os.environ["HOME"] 落在 workspace 下）。
5. 不测解释器逃逸（`python -c "open('C:/x')"` 是已知边界，写进来只会是一条永红用例）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jagent.contracts import EventType, Plan, Step

from .conftest import make_runner

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义，G-06）；单用户本地 Harness 是 Windows",
)

SECRET = "TOPSECRET-OUTSIDE-WORKSPACE"

#: 每次真实执行的硬上限（秒）。需求要求单测 5 秒内跑完；UNC 路径未拦截时会阻塞，
#: 这里用 subprocess 超时把它压住，让用例失败而不是超时。
SHELL_TIMEOUT_S = 3.5


@pytest.fixture()
def outside(shell_bench):
    """workspace 外的一个真文件：被读到就说明沙箱没生效。"""
    path = shell_bench.root / "secret.txt"
    path.write_text(SECRET, encoding="utf-8")
    return path


def shell(bench, cmd: str, *, timeout: float = SHELL_TIMEOUT_S):
    return bench.registry.call("shell.exec", {"cmd": cmd}, workspace=bench.ws, timeout=timeout)


# --------------------------------------------------------------------------- #
# 1 · 命令体不能读写 workspace 外的文件
# --------------------------------------------------------------------------- #

_MUST_BE_DENIED = [
    ("parent_traversal", r"type ..\secret.txt"),
    ("double_parent", r"type ..\..\secret.txt"),
    ("windows_dir", r"type C:\Windows\win.ini"),
    ("unc_share", r"type \\server\share\x.txt"),
    ("user_profile_env", r"type %USERPROFILE%\secret.txt"),
    ("home_env", r"type $HOME/secret.txt"),
]


@pytest.mark.parametrize(
    ("label", "cmd"), _MUST_BE_DENIED, ids=[label for label, _ in _MUST_BE_DENIED]
)
def test_g06_01_command_body_cannot_reach_outside_workspace(shell_bench, outside, label, cmd):
    result = shell(shell_bench, cmd)

    assert result.ok is False, f"[{label}] 命令体越出 workspace 仍被执行: {cmd!r}"
    assert result.error_code == "sandbox_denied", (
        f"[{label}] 期望 sandbox_denied，实际 {result.error_code!r}: {result.error_message}"
    )
    assert SECRET not in (result.stdout or ""), f"[{label}] 外部文件内容漏进了 stdout"
    assert SECRET not in repr(result.data), f"[{label}] 外部文件内容漏进了返回数据"


def test_g06_01i_absolute_path_to_outside_file_is_denied(shell_bench, outside):
    """绝对路径的真文件：被读到就会在 stdout 里留下明文。"""
    result = shell(shell_bench, f'type "{outside}"')

    assert result.ok is False, f"绝对路径读到了 workspace 外的文件: {outside}"
    assert result.error_code == "sandbox_denied"
    assert SECRET not in (result.stdout or "")
    assert SECRET not in (result.stderr or "")


# --------------------------------------------------------------------------- #
# 2 · 重定向到外部也要拒
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("label", "cmd", "target"),
    [
        ("redirect_write_parent", r"echo pwned > ..\stolen.txt", "stolen.txt"),
        ("redirect_steal", r"type ..\secret.txt > ..\leak.txt", "leak.txt"),
    ],
)
def test_g06_02_redirect_outside_workspace_is_denied(shell_bench, outside, label, cmd, target):
    result = shell(shell_bench, cmd)

    assert result.error_code == "sandbox_denied", f"[{label}] 重定向到外部没有被拒"
    assert not (shell_bench.root / target).exists(), f"[{label}] 被拒之后外部文件仍然出现了"


# --------------------------------------------------------------------------- #
# 3 · 合法命令要能跑
# --------------------------------------------------------------------------- #

def _legal_cmd(label: str) -> str:
    if label == "python_print":
        return f'"{sys.executable}" -c "print(1)"'
    if label == "pytest_version":
        # 用解释器模块形式，避免把 venv Scripts 目录塞进 PATH 才能跑起来。
        return f'"{sys.executable}" -m pytest --version'
    return "git status"


@pytest.mark.parametrize("label", ["python_print", "pytest_version", "git_status"])
def test_g06_03_legal_commands_still_run(shell_bench, label):
    result = shell(shell_bench, _legal_cmd(label))

    assert result.ok is True, (
        f"[{label}] 合法命令被拦了: {result.error_code!r} {result.error_message}"
    )
    assert result.error_code is None
    assert result.exit_code is not None, f"[{label}] 没有真的跑起来（没有退出码）"

    if label == "python_print":
        assert result.exit_code == 0
        assert "1" in result.stdout
    elif label == "pytest_version":
        assert result.exit_code == 0, result.stderr
        assert "pytest" in (result.stdout or "").lower()
    else:
        # 空 workspace 不是 git 仓库：退出码 128 + fatal 才是"真跑过"的证据。
        assert result.exit_code == 0 or "fatal" in (result.stderr or "").lower(), (
            f"git status 没有真的执行: exit={result.exit_code} stderr={result.stderr!r}"
        )


# --------------------------------------------------------------------------- #
# 4 · scrub_env
# --------------------------------------------------------------------------- #

_ENV_PROBE = (
    "import os\n"
    "for key in ('HOME', 'TEMP'):\n"
    "    print(key + '=' + os.environ.get(key, '<unset>'))\n"
)


def test_g06_04_scrub_env_keeps_home_and_temp_inside_workspace(shell_bench):
    (shell_bench.ws / "probe_env.py").write_text(_ENV_PROBE, encoding="utf-8")

    result = shell(shell_bench, f'"{sys.executable}" probe_env.py')

    assert result.ok is True, f"合法命令被拦了: {result.error_code!r}"
    assert result.exit_code == 0, result.stderr

    values = dict(
        line.split("=", 1) for line in (result.stdout or "").splitlines() if "=" in line
    )
    ws = shell_bench.ws.resolve()
    for key in ("HOME", "TEMP"):
        raw = values.get(key, "")
        assert raw and raw != "<unset>", f"子进程里没有 {key}：scrub_env 没有注入这个变量"
        target = Path(raw).resolve()
        assert target == ws or ws in target.parents, f"{key}={raw} 没有落在 workspace 内"


# --------------------------------------------------------------------------- #
# 5 · 拒绝要成为一条可审计的确定性失败，且不留下外部内容
# --------------------------------------------------------------------------- #

def test_g06_05_denial_is_deterministic_and_leaks_nothing(shell_bench, outside):
    runner = make_runner(shell_bench, auto_approve={"shell.exec"})
    plan = Plan(steps=(Step(id="peek", tool="shell.exec", args={"cmd": r"type ..\secret.txt"}),))

    outcome = runner.start_and_run(plan)

    assert outcome.status == "failed", "读到外部文件还算通过，说明沙箱没有生效"
    classified = shell_bench.of_type(EventType.FAILURE_CLASSIFIED, run_id=outcome.run_id)
    assert classified, "拒绝必须被分类（否则重试策略会把它当瞬时错误反复重试）"
    assert classified[-1].payload["class"] == "deterministic"
    assert classified[-1].payload["error_code"] == "sandbox_denied"

    for event in shell_bench.of_type(EventType.STEP_FINISHED, run_id=outcome.run_id):
        assert SECRET not in repr(event.payload), "外部文件内容进了 STEP_FINISHED 观测"
    artifacts = shell_bench.root / "artifacts"
    for path in artifacts.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), f"外部文件内容落进了产物 {path}"
