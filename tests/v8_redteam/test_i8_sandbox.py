"""I8 · 沙箱：路径与 cwd 不能逃出 workspace，逃逸必须 sandbox_denied。

规格依据：
- §4.7 Executor「沙箱 + 最小权限」；
- §5.1「超出沙箱 / 权限不够 → Executor 拒绝执行 → E12 → E14（确定性）」；
- contracts.ERROR_CLASS：``sandbox_denied`` 属于 deterministic；
- docs/jagent-v8-mvp.md §6.1：「生产要有沙箱（现在只有 cwd + timeout）」。
"""

from __future__ import annotations

import sys

import pytest

from jagent.contracts import EventType, Plan, Step, Verify, classify_error, FailureClass
from jagent.contracts import SideEffect, ToolSpec, Idempotency
from jagent.harness.registry import ToolRegistry, build_default_registry
from jagent.harness.executor import Executor
from jagent.harness.artifacts import ArtifactStore

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义，G-06）；单用户本地 Harness 是 Windows",
)


@pytest.fixture()
def sandbox(harness):
    secret = harness.root / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    registry = build_default_registry(harness.ws)
    return harness, registry, secret


def test_fs_read_parent_traversal_is_denied(sandbox) -> None:
    harness, registry, _secret = sandbox

    result = registry.call("fs.read", {"path": "../secret.txt"}, workspace=harness.ws)

    assert result.ok is False
    assert result.error_code == "sandbox_denied"
    assert "TOPSECRET" not in repr(result.data)


def test_fs_read_absolute_path_is_denied(sandbox) -> None:
    harness, registry, secret = sandbox

    result = registry.call("fs.read", {"path": str(secret)}, workspace=harness.ws)

    assert result.ok is False
    assert result.error_code == "sandbox_denied"
    assert "TOPSECRET" not in repr(result.data)


def test_fs_list_parent_is_denied(sandbox) -> None:
    harness, registry, _secret = sandbox

    result = registry.call("fs.list", {"path": ".."}, workspace=harness.ws)

    assert result.ok is False
    assert result.error_code == "sandbox_denied"


def test_fs_write_escape_is_denied_and_creates_nothing(sandbox) -> None:
    harness, registry, _secret = sandbox
    target = harness.root / "escaped.txt"

    result = registry.call(
        "fs.write", {"path": "../escaped.txt", "content": "pwned"}, workspace=harness.ws
    )

    assert result.ok is False
    assert result.error_code == "sandbox_denied"
    assert not target.exists(), "沙箱拒绝之后文件不能被创建"


def test_fs_write_inside_workspace_still_works(sandbox) -> None:
    """正向对照：不能因为防护过严把正常写入也拦了。"""

    harness, registry, _secret = sandbox

    result = registry.call(
        "fs.write", {"path": "ok.txt", "content": "hello"}, workspace=harness.ws
    )

    assert result.ok is True
    assert (harness.ws / "ok.txt").read_text(encoding="utf-8") == "hello"


def test_shell_exec_cwd_outside_workspace_is_denied(sandbox) -> None:
    harness, registry, _secret = sandbox

    parent = registry.call("shell.exec", {"cmd": "echo hi", "cwd": ".."}, workspace=harness.ws)
    absolute = registry.call(
        "shell.exec", {"cmd": "echo hi", "cwd": str(harness.root)}, workspace=harness.ws
    )

    assert parent.ok is False and parent.error_code == "sandbox_denied"
    assert absolute.ok is False and absolute.error_code == "sandbox_denied"


def test_sandbox_denied_is_classified_deterministic() -> None:
    assert classify_error("sandbox_denied") is FailureClass.DETERMINISTIC


def test_sandbox_denied_fails_the_run_without_reading_outside(harness) -> None:
    from tests.v8_redteam.conftest import make_runner

    secret = harness.root / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    registry = build_default_registry(harness.ws)
    runner = make_runner(harness, registry=registry)
    plan = Plan(
        steps=(
            Step(
                id="peek",
                tool="fs.read",
                args={"path": "../secret.txt"},
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        )
    )

    outcome = runner.start_and_run(plan)

    assert outcome.status == "failed"
    assert outcome.result is not None and outcome.result.verdict.value == "failed"
    classified = harness.of_type(EventType.FAILURE_CLASSIFIED)
    assert classified and classified[-1].payload["class"] == "deterministic"
    finished = harness.of_type(EventType.STEP_FINISHED)
    assert finished
    assert "TOPSECRET" not in repr(finished[-1].payload["observations"])
    # 产物库同样不能留下外部文件内容。
    for path in (harness.root / "artifacts").rglob("*"):
        if path.is_file():
            assert b"TOPSECRET" not in path.read_bytes()


@pytest.mark.known_bug(
    id="RT-08",
    reason="shell.exec 只限制 cwd，不限制命令体：命令可用绝对路径读 workspace 外的文件",
)
def test_shell_exec_command_cannot_read_outside_workspace(sandbox) -> None:
    harness, registry, secret = sandbox
    cmd = f'"{sys.executable}" -c "print(open(r\'{secret.as_posix()}\').read())"'

    result = registry.call("shell.exec", {"cmd": cmd, "cwd": "."}, workspace=harness.ws)

    assert "TOPSECRET" not in (result.stdout or ""), (
        "cwd 限制只约束工作目录，命令体仍可读任意绝对路径；workspace 逃逸没有被真正阻断"
    )


def test_irreversible_tool_requires_approval_before_running(harness) -> None:
    """不可逆步骤没有 gated 也必须先问人（ToolSpec.idempotency 注释：NONE 必须 gated）。"""

    from tests.v8_redteam.conftest import make_runner

    marker = harness.ws / "marker.txt"
    registry = ToolRegistry()
    executed: list[int] = []

    def shell(args, workspace, timeout):
        executed.append(1)
        marker.write_text("ran", encoding="utf-8")
        from jagent.contracts import ToolResult

        return ToolResult(ok=True, exit_code=0, stdout="")

    registry.add(
        ToolSpec(
            name="shell.exec",
            version="1",
            args_schema={"type": "object"},
            side_effect=SideEffect.IRREVERSIBLE,
            idempotency=Idempotency.NONE,
            observations=("ok", "exit_code", "stdout", "stderr"),
        ),
        shell,
    )
    executor = Executor(registry, ArtifactStore(harness.root / "artifacts"), harness.writer,
                        workspace=harness.ws)
    runner = make_runner(harness, registry=registry, executor=executor, auto_approve=frozenset())
    plan = Plan(
        steps=(
            Step(
                id="run",
                tool="shell.exec",
                args={"cmd": "do-something"},
                verify=Verify("exit_code == 0", needs=("exit_code",)),
                gated=False,
            ),
        )
    )

    outcome = runner.start_and_run(plan)

    assert outcome.status == "waiting_human", "不可逆工具未 gated 时也必须挂起等人"
    assert executed == []
    assert not marker.exists()
