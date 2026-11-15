"""JAgent v8 · 执行侧（registry / executor / verify / classify）。

这些测试只碰 public 面：注册表、Executor.run_step、Verifier.verify、classify。
断言全部落在「可回放的事实」上——事件 payload、artifact 内容、StepOutcome 的观测，
而不是内部变量。工具用的是真实文件系统的 `tmp_path` 工作区，不 mock。
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from jagent.contracts import (
    ERROR_CLASS,
    EventType,
    FailureClass,
    Plan,
    Step,
    StepOutcome,
    Verify,
    VerifyOutcome,
    digest_of,
    plan_to_dict,
)
from jagent.harness.artifacts import ArtifactStore
from jagent.harness.classify import classify
from jagent.harness.executor import Executor
from jagent.harness.fold import World
from jagent.harness.registry import build_default_registry
from jagent.harness.store import EventStore
from jagent.harness.verify import Verifier
from jagent.harness.writer import Writer


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@dataclass
class Env:
    store: EventStore
    writer: Writer
    executor: Executor
    artifacts: ArtifactStore
    workspace: Path
    plan: Plan
    run_id: str = "run_test"

    def step(self, step_id: str) -> Step:
        return self.plan.by_id()[step_id]

    def events(self, type_: EventType) -> list:
        return [e for e in self.store.read(run_id=self.run_id) if e.type is type_]


@pytest.fixture
def make_env(tmp_path: Path):
    """按给定的步骤造一个「已经冻结计划」的 run，返回可执行的环境。"""
    opened: list[EventStore] = []

    def build(steps: tuple[Step, ...]) -> Env:
        workspace = tmp_path / "ws"
        workspace.mkdir(parents=True, exist_ok=True)
        store = EventStore(tmp_path / "events.db")
        opened.append(store)
        writer = Writer(store, World(), "sess_test")
        plan = Plan(steps=steps)
        writer.emit(
            EventType.RUN_STARTED,
            {"run_id": "run_test", "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
            run_id="run_test",
        )
        writer.emit(
            EventType.PLAN_FROZEN,
            {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": plan.version},
            run_id="run_test",
        )
        artifacts = ArtifactStore(tmp_path / "artifacts")
        executor = Executor(
            build_default_registry(workspace), artifacts, writer, workspace=workspace
        )
        return Env(
            store=store, writer=writer, executor=executor, artifacts=artifacts,
            workspace=workspace, plan=plan,
        )

    yield build
    for store in opened:
        store.close()  # Windows 上不关掉，tmp_path 清理会撞上打开的文件句柄


def _py(code: str) -> str:
    """用当前解释器执行一段代码：跨平台，且不依赖 PATH 上有没有 python。

    代码里不能出现双引号（cmd.exe 的引号规则比 POSIX 苛刻）。
    """
    return f'"{sys.executable}" -c "{code}"'


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

def test_registry_declares_four_tools(tmp_path: Path) -> None:
    registry = build_default_registry(tmp_path)

    assert registry.names() == ["fs.read", "fs.list", "fs.write", "shell.exec"]
    for name in registry.names():
        spec = registry.get(name)
        assert spec.name == name
        assert spec.error_codes, "空错误面等于没声明错误码"
        for code in spec.error_codes:
            assert code in ERROR_CLASS, f"{name} 声明了分类器不认识的错误码 {code!r}"

    descriptors = registry.descriptors()
    assert [d["name"] for d in descriptors] == registry.names()
    assert set(descriptors[0]) == {
        "name", "args", "side_effect", "readonly", "error_codes", "observations", "description",
    }
    # Planner 的 needs ⊆ provides 就靠这个字段：每个工具必须声明自己产出哪些观测
    for spec in (registry.get(n) for n in registry.names()):
        assert spec.observations, f"{spec.name} 没声明观测名"

    with pytest.raises(KeyError):
        registry.get("fs.remove")
    with pytest.raises(KeyError):
        registry.call("fs.remove", {}, workspace=tmp_path)


def test_inline_fallbacks_when_legacy_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    """旧仓库缺席时，四个工具必须仍然工作，而且仍然守着 workspace 边界。"""
    from jagent.harness import registry as registry_module

    monkeypatch.setattr(registry_module, "_legacy_tools", lambda workspace: None)
    registry = build_default_registry(tmp_path)
    (tmp_path / "here.txt").write_text("inline", encoding="utf-8")

    assert registry.call("fs.read", {"path": "here.txt"}, workspace=tmp_path).data == {
        "content": "inline"
    }
    escaped = registry.call("fs.read", {"path": "../outside.txt"}, workspace=tmp_path)
    assert escaped.ok is False
    assert escaped.error_code == "sandbox_denied"
    listed = registry.call("fs.list", {}, workspace=tmp_path)
    assert listed.ok is True
    assert [e["name"] for e in listed.data["entries"]] == ["here.txt"]
    wrote = registry.call(
        "fs.write", {"path": "new.txt", "content": "abc"}, workspace=tmp_path
    )
    assert wrote.ok is True
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "abc"


# --------------------------------------------------------------------------- #
# fs.read
# --------------------------------------------------------------------------- #

def test_fs_read_returns_content_and_artifact(make_env) -> None:
    env = make_env((Step(id="s1", tool="fs.read", args={"path": "notes.txt"}),))
    (env.workspace / "notes.txt").write_text("hello v8\n", encoding="utf-8")
    step = env.step("s1")

    outcome = env.executor.run_step(env.run_id, step)

    assert outcome.ok is True
    assert outcome.error_code is None
    assert outcome.observations["content"] == "hello v8\n"
    assert outcome.observations["exit_code"] is None
    assert outcome.observations["ok"] is True
    assert len(outcome.artifact_refs) == 1

    # artifact 里是 data 的 JSON 全文，不是观测的拷贝
    stored = json.loads(env.artifacts.get_text(outcome.artifact_refs[0]))
    assert stored == {"content": "hello v8\n"}

    # 身份：idempotency_key 只由 run_id / step_id / args_hash 决定
    args_hash = digest_of({"path": "notes.txt"})
    expected = hashlib.sha256(f"run_test|s1|{args_hash}".encode("utf-8")).hexdigest()
    started = env.events(EventType.STEP_STARTED)
    assert [e.payload["idempotency_key"] for e in started] == [expected]
    assert started[0].payload["attempt"] == 1

    # 顺序：先 started，再 artifact，最后 finished
    seq = {e.type: e.seq for e in env.store.read(run_id=env.run_id)
           if e.type in {EventType.STEP_STARTED, EventType.ARTIFACT_WRITTEN, EventType.STEP_FINISHED}}
    assert seq[EventType.STEP_STARTED] < seq[EventType.ARTIFACT_WRITTEN] < seq[EventType.STEP_FINISHED]

    written = env.events(EventType.ARTIFACT_WRITTEN)[0].payload
    assert written["artifact_id"] == outcome.artifact_refs[0]
    assert written["sha256"] == written["artifact_id"]
    assert written["bytes"] > 0
    assert written["mime"] == "application/json"

    finished = env.events(EventType.STEP_FINISHED)[0].payload
    assert finished["status"] == "ok"
    assert finished["artifact_refs"] == list(outcome.artifact_refs)
    assert finished["observations"] == outcome.observations


def test_fs_read_out_of_scope_is_sandbox_denied(make_env) -> None:
    env = make_env((Step(id="s1", tool="fs.read", args={"path": "../escape.txt"}),))
    (env.workspace.parent / "escape.txt").write_text("secret", encoding="utf-8")

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    assert outcome.ok is False
    assert outcome.error_code == "sandbox_denied"
    assert "escape" in outcome.error_message
    assert outcome.artifact_refs == ()  # 失败的调用没有可存的内容
    assert classify(outcome, VerifyOutcome(passed=True)) is FailureClass.DETERMINISTIC
    assert env.events(EventType.STEP_FINISHED)[0].payload["status"] == "failed"


def test_fs_read_absolute_path_is_also_out_of_scope(make_env, tmp_path: Path) -> None:
    target = tmp_path / "absolute.txt"
    target.write_text("secret", encoding="utf-8")
    env = make_env((Step(id="s1", tool="fs.read", args={"path": str(target)}),))

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    assert outcome.ok is False
    assert outcome.error_code == "sandbox_denied"


def test_fs_read_missing_file_is_not_found(make_env) -> None:
    env = make_env((Step(id="s1", tool="fs.read", args={"path": "nope.txt"}),))

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    assert outcome.ok is False
    assert outcome.error_code == "not_found"
    assert classify(outcome, VerifyOutcome(passed=True)) is FailureClass.DETERMINISTIC


# --------------------------------------------------------------------------- #
# fs.list / fs.write
# --------------------------------------------------------------------------- #

def test_fs_list_returns_entries(make_env) -> None:
    env = make_env((Step(id="s1", tool="fs.list", args={}),))
    (env.workspace / "a.txt").write_text("x", encoding="utf-8")
    (env.workspace / "sub").mkdir()

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    assert outcome.ok is True
    entries = {e["name"]: e["kind"] for e in outcome.observations["entries"]}
    assert entries == {"a.txt": "file", "sub": "dir"}


def test_fs_write_creates_missing_file(make_env) -> None:
    env = make_env((
        Step(id="s1", tool="fs.write", args={"path": "made/new.txt", "content": "v8"}),
    ))
    (env.workspace / "made").mkdir()

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    # 旧 WriteFileTool 只会覆盖已存在文件；新建由 registry 兜底
    assert outcome.ok is True, outcome.error_message
    assert (env.workspace / "made" / "new.txt").read_text(encoding="utf-8") == "v8"
    assert outcome.observations["bytes"] == 2
    assert outcome.observations["path"] == "made/new.txt"


def test_fs_write_missing_parent_is_not_found(make_env) -> None:
    env = make_env((
        Step(id="s1", tool="fs.write", args={"path": "ghost/new.txt", "content": "v8"}),
    ))

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    assert outcome.ok is False
    assert outcome.error_code == "not_found"
    assert not (env.workspace / "ghost").exists()


# --------------------------------------------------------------------------- #
# shell.exec
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义）；单用户本地 Harness 是 Windows",
)
def test_shell_exec_captures_stdout(make_env) -> None:
    env = make_env((Step(id="s1", tool="shell.exec", args={"cmd": _py("print(123)")}),))

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    assert outcome.ok is True
    assert outcome.observations["exit_code"] == 0
    assert outcome.observations["stdout"].strip() == "123"
    assert any(env.artifacts.get_text(ref) for ref in outcome.artifact_refs)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义）；单用户本地 Harness 是 Windows",
)
def test_shell_exec_nonzero_exit_is_an_observation(make_env) -> None:
    env = make_env((
        Step(
            id="s1",
            tool="shell.exec",
            args={"cmd": _py("import sys; sys.exit(3)")},
            verify=Verify(expr="exit_code == 3"),
        ),
    ))
    step = env.step("s1")

    outcome = env.executor.run_step(env.run_id, step)

    # 非零退出不是工具失败：工具成功，观测里是 3
    assert outcome.ok is True
    assert outcome.observations["exit_code"] == 3
    assert Verifier().verify(step, outcome).passed is True
    assert classify(outcome, Verifier().verify(step, outcome)) is None

    # 换一条断言同样的执行就不过 —— 判不判由断言决定，不由退出码决定
    strict = Step(id="s1", tool="shell.exec", args=step.args, verify=Verify(expr="exit_code == 0"))
    verdict = Verifier().verify(strict, outcome)
    assert verdict.passed is False
    assert classify(outcome, verdict) is FailureClass.ASSERTION


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义）；单用户本地 Harness 是 Windows",
)
def test_shell_exec_timeout_is_transient(tmp_path: Path) -> None:
    registry = build_default_registry(tmp_path)
    # 子进程睡得比 timeout 长；杀掉 cmd.exe 后它的 python 子进程仍握着管道，
    # 所以 sleep 放小，别让清理阶段变成测试的墙钟时间。
    cmd = _py("import time; time.sleep(1.5)")

    result = registry.call("shell.exec", {"cmd": cmd}, workspace=tmp_path, timeout=0.25)

    assert result.ok is False
    assert result.error_code == "timeout"
    assert classify(StepOutcome(ok=False, error_code=result.error_code), VerifyOutcome(passed=False)) is \
        FailureClass.TRANSIENT


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="shell.exec 走平台 shell（Windows cmd 语义）；单用户本地 Harness 是 Windows",
)
def test_long_stdout_is_truncated_in_event_and_full_in_artifact(make_env) -> None:
    env = make_env((Step(id="s1", tool="shell.exec", args={"cmd": _py("print(chr(120)*9000)")}),))

    outcome = env.executor.run_step(env.run_id, env.step("s1"))

    assert outcome.ok is True
    assert len(outcome.observations["stdout"]) == 4000

    finished = env.events(EventType.STEP_FINISHED)[0].payload
    assert len(finished["observations"]["stdout"]) == 4000

    full = max((env.artifacts.get_text(ref) for ref in outcome.artifact_refs), key=len)
    assert full.startswith("x" * 9000)


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def _outcome(**observations) -> StepOutcome:
    return StepOutcome(ok=True, observations=dict(observations))


def test_verify_passes_and_fails_on_observations() -> None:
    verifier = Verifier()
    step = Step(
        id="s1",
        tool="shell.exec",
        verify=Verify(expr='ok and exit_code == 0 and "hello" in stdout'),
    )

    good = verifier.verify(step, _outcome(ok=True, exit_code=0, stdout="hello world", stderr=""))
    assert good.passed is True
    assert good.reason == ""
    assert len(good.checks) == 1
    assert good.checks[0]["expr"] == step.verify.expr
    assert good.checks[0]["passed"] is True

    bad = verifier.verify(step, _outcome(ok=True, exit_code=2, stdout="", stderr="boom"))
    assert bad.passed is False
    assert bad.checks[0]["passed"] is False
    assert bad.reason.startswith("assertion_failed")


def test_verify_supports_len_in_and_or_not() -> None:
    verifier = Verifier()
    step = Step(
        id="s1",
        tool="fs.read",
        verify=Verify(
            expr='len(content) > 3 and ("hello" in content or "hi" in content) '
                 'and not ("fatal" in content) and abs(exit_code - 0) == 0',
            needs=("content", "exit_code"),
        ),
    )

    verdict = verifier.verify(step, _outcome(content="hello v8", exit_code=0))

    assert verdict.passed is True, verdict.reason


def test_verify_accepts_json_style_literals() -> None:
    """Planner 在 JSON 语境里写 `ok == true` / `x == null` 必须能求值。

    回归：按 Python 语法 `true` 是名字，曾被判成「观测缺失」而假失败
    （真实 E2E 里 fs.read 读成功、run 却被判 failed）。
    """
    verifier = Verifier()

    step = Step(
        id="s1",
        tool="fs.read",
        verify=Verify(expr='ok == true and len(content) > 0', needs=("ok", "content")),
    )
    assert verifier.verify(step, _outcome(ok=True, content="hello")).passed is True

    # 字符串字面量里的 true 不受影响
    quoted = Step(
        id="s2",
        tool="shell.exec",
        verify=Verify(expr='"true" in stdout', needs=("stdout",)),
    )
    assert verifier.verify(quoted, _outcome(stdout="value is true")).passed is True

    null_check = Step(
        id="s3",
        tool="fs.read",
        verify=Verify(expr="stderr == null", needs=("stderr",)),
    )
    assert verifier.verify(null_check, _outcome(stderr=None)).passed is True


def test_verify_missing_evidence_fails() -> None:
    verifier = Verifier()
    # 引用了一个观测里没有的名字
    step = Step(id="s1", tool="fs.read", verify=Verify(expr="len(stdout) > 0"))

    verdict = verifier.verify(step, _outcome(content="hello"))

    assert verdict.passed is False
    assert verdict.reason.startswith("evidence_missing")
    assert "stdout" in verdict.reason
    assert verdict.checks[0]["passed"] is False


def test_verify_declared_needs_are_enforced() -> None:
    verifier = Verifier()
    # 表达式本身不引用 stderr，但 needs 声明了要读它 —— 读不到就是证据缺失
    step = Step(
        id="s1",
        tool="shell.exec",
        verify=Verify(expr="ok and exit_code == 0", needs=("stderr",)),
    )

    verdict = verifier.verify(step, _outcome(ok=True, exit_code=0))

    assert verdict.passed is False
    assert verdict.reason.startswith("evidence_missing")
    assert "stderr" in verdict.reason


def test_verify_rejects_unsafe_or_broken_expressions() -> None:
    verifier = Verifier()

    for expr in ('ok and', "ok.__class__", "open('x')", "content[0] == 'a'", "lambda: 1"):
        verdict = verifier.verify(
            Step(id="s1", tool="fs.read", verify=Verify(expr=expr)),
            _outcome(ok=True, content="abc"),
        )
        assert verdict.passed is False, expr
        assert verdict.reason.startswith("eval_error"), (expr, verdict.reason)
        assert verdict.checks[0]["passed"] is False


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #

def test_classify_three_branches() -> None:
    ok = StepOutcome(ok=True, observations={"ok": True})

    assert classify(ok, VerifyOutcome(passed=True)) is None
    assert classify(StepOutcome(ok=False, error_code="timeout"), VerifyOutcome(passed=False)) is \
        FailureClass.TRANSIENT
    assert classify(StepOutcome(ok=False, error_code="not_found"), VerifyOutcome(passed=True)) is \
        FailureClass.DETERMINISTIC
    assert classify(ok, VerifyOutcome(passed=False)) is FailureClass.ASSERTION
    # 工具失败优先于断言失败：断言不过是工具失败的后果
    assert classify(StepOutcome(ok=False, error_code="timeout"), VerifyOutcome(passed=False)) is \
        FailureClass.TRANSIENT


# --------------------------------------------------------------------------- #
# 步骤间取值：{{step_id.path}}
# --------------------------------------------------------------------------- #

def test_executor_binds_previous_step_observations(make_env) -> None:
    env = make_env(
        (
            Step(id="s1", tool="fs.read", args={"path": "a.txt"},
                 verify=Verify("len(content) > 0", needs=("content",))),
            Step(id="s2", tool="fs.write", args={"path": "b.txt", "content": "{{s1.content}}"},
                 depends_on=("s1",), verify=Verify("ok", needs=("ok",))),
        )
    )
    (env.workspace / "a.txt").write_text("hello-binding", encoding="utf-8")

    assert env.executor.run_step(env.run_id, env.step("s1")).ok is True
    second = env.executor.run_step(env.run_id, env.step("s2"))

    assert second.ok is True, second.error_message
    assert (env.workspace / "b.txt").read_text(encoding="utf-8") == "hello-binding"


def test_executor_binding_failure_is_invalid_args(make_env) -> None:
    env = make_env(
        (
            Step(id="s1", tool="fs.read", args={"path": "a.txt"}),
            Step(id="s2", tool="fs.write", args={"path": "b.txt", "content": "{{s1.nope}}"},
                 depends_on=("s1",)),
        )
    )
    (env.workspace / "a.txt").write_text("x", encoding="utf-8")
    env.executor.run_step(env.run_id, env.step("s1"))

    second = env.executor.run_step(env.run_id, env.step("s2"))

    assert second.ok is False
    assert second.error_code == "invalid_args"
    assert "binding failed" in second.error_message
    assert not (env.workspace / "b.txt").exists()
