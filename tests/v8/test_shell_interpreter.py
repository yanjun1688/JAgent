"""`shell.exec` 里的解释器必须和跑服务的解释器是同一个。

背景（根因测试）：`python -m jagent.api` 起服务后说「跑回归测试」，计划里那条命令是
`python -m pytest tests -q`。它继承 PATH，用的是 PATH 上碰巧排第一的 python；
如果那个 python 没装 pytest，产品的主路径就必然失败——而用户没有任何办法指定解释器
（沙箱按设计拒绝绝对路径）。

所以契约是：**命令里的 `python` 就是跑 JAgent 的这个 python**。
"""

from __future__ import annotations

import os
import sys
import pytest
from pathlib import Path

from jagent.harness.registry import build_default_registry
from jagent.harness.sandbox import _path_key, scrub_env


def test_scrubbed_path_puts_the_service_interpreter_first(tmp_path: Path) -> None:
    env = scrub_env(workspace=tmp_path)
    first = env[_path_key(env)].split(os.pathsep)[0]

    assert Path(first).resolve() == Path(sys.executable).resolve().parent


def test_the_interpreter_appears_once_in_path(tmp_path: Path) -> None:
    env = scrub_env(workspace=tmp_path)
    entries = [Path(part).resolve() for part in env[_path_key(env)].split(os.pathsep) if part]

    assert entries.count(Path(sys.executable).resolve().parent) == 1


def test_shell_python_is_the_service_python(tmp_path: Path) -> None:
    registry = build_default_registry(tmp_path)

    result = registry.call(
        "shell.exec",
        {"cmd": 'python -c "print(__import__(\'sys\').executable)"'},
        workspace=tmp_path,
    )

    assert result.ok is True, result.error_message
    assert result.exit_code == 0
    assert Path(result.stdout.strip()).resolve() == Path(sys.executable).resolve()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义）；单用户本地 Harness 是 Windows",
)
def test_shell_can_import_what_the_service_has(tmp_path: Path) -> None:
    """pytest 在服务解释器里装好了，子进程就该看得见它。"""
    registry = build_default_registry(tmp_path)

    result = registry.call("shell.exec", {"cmd": "python -m pytest --version"}, workspace=tmp_path)

    assert result.exit_code == 0, result.stderr
