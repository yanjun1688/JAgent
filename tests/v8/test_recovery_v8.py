"""v8 · G-05 恢复对账闭环（真 EventStore + 手工/半手工事件模拟崩溃）。

四个缺口，四组可执行断言：

1. **环境指纹**：`plan.frozen` 里记 `env_fp`；每次恢复前重算并发 `env.fingerprinted`；
   变了**只**失效「已 passed 且产物来自 workspace」的步骤（`step.invalidated{reason:"drift"}`）。
2. **payload_hash 执行前复核**：批准之后参数被改 → 不执行，重新 `approval.requested`。
3. **in-doubt**（进程死在 `step.started` 之后）：按 `ToolSpec.idempotency` 分流；
   `NONE`（shell.exec）转人工，`NATURAL`/`KEYED` 复用同一个幂等键重跑。
4. **恢复幂等**：连续两次 resume，第二次是 no-op（不重复执行、不重复发 token）。

不 mock 事件流：每个用例都是真 sqlite `EventStore` + 真 `fold` + 真 `Writer`。
"崩溃"有两种造法，都用得到：
- `Crash(BaseException)` 穿过 `Executor` 的 `except Exception`，于是 `step.started`
  落盘、`step.finished` 不落盘——这是最接近 `kill -9` 的形态；
- 直接手工写 `step.started`，用于不需要真跑工具的场景（例如不可逆命令）。
两种都不用 sleep。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import pytest

from jagent.app.runner import Runner
from jagent.contracts import (
    Budget,
    EventType,
    Idempotency,
    Plan,
    SideEffect,
    Step,
    ToolResult,
    ToolSpec,
    Verify,
    digest_of,
    plan_to_dict,
)
from jagent.harness.artifacts import ArtifactStore
from jagent.harness.executor import Executor
from jagent.harness.fingerprint import probe_fingerprints, snapshot_workspace
from jagent.harness.fold import World, fold
from jagent.harness.human import HumanCenter
from jagent.harness.reconcile import (
    RETRY_KEYED,
    RETRY_NATURAL,
    in_doubt_steps,
    last_attempt,
    reconcile_in_doubt,
)
from jagent.harness.registry import ToolRegistry, build_default_registry
from jagent.harness.repair import HeuristicRepairer
from jagent.harness.report import Reporter
from jagent.harness.store import EventStore
from jagent.harness.verify import Verifier
from jagent.harness.writer import Writer

SESSION_ID = "s-recovery-v8"


class Crash(BaseException):
    """模拟进程被杀。

    故意继承 `BaseException`：`Executor._invoke` 只兜 `Exception`，
    所以这条异常会穿过执行器，让现场停在「`step.started` 已写、`step.finished` 没写」。
    """


# --------------------------------------------------------------------------- #
# 夹具：真 store + 真 fold + 真 Writer + 自己的 workspace
# --------------------------------------------------------------------------- #

@dataclass
class Rig:
    root: Path
    ws: Path
    store: EventStore
    writer: Writer
    registry: ToolRegistry
    human: HumanCenter
    executor: Executor

    @property
    def world(self) -> World:
        return self.writer.world

    def reload(self) -> World:
        """模拟进程重启：只从事件日志重建世界（零模型、零内存残留）。"""
        self.writer.world = fold(self.store.read())
        return self.writer.world

    def runner(self, *, auto_approve: frozenset[str] = frozenset()) -> Runner:
        """新 Runner（同一个 writer，除非调用方先 reload）。"""
        return Runner(
            writer=self.writer,
            executor=self.executor,
            verifier=Verifier(),
            repairer=HeuristicRepairer(),
            reporter=Reporter(),
            human=self.human,
            budget=Budget(),
            registry=self.registry,
            auto_approve=frozenset(auto_approve),
        )

    def events(self, run_id: str | None = None) -> list:
        return self.store.read(run_id=run_id) if run_id else self.store.read()

    def of_type(self, event_type: EventType, run_id: str | None = None) -> list:
        return [event for event in self.events(run_id) if event.type is event_type]


@pytest.fixture()
def rig(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = EventStore(tmp_path / "events.db")
    writer = Writer(store, fold(store.read()), SESSION_ID)
    registry = build_default_registry(ws)
    executor = Executor(registry, ArtifactStore(tmp_path / "artifacts"), writer, workspace=ws)
    item = Rig(
        root=tmp_path,
        ws=ws,
        store=store,
        writer=writer,
        registry=registry,
        human=HumanCenter(writer),
        executor=executor,
    )
    yield item
    store.close()


# --------------------------------------------------------------------------- #
# 工具与计划的小工具
# --------------------------------------------------------------------------- #

def _probe_spec(
    name: str = "probe",
    *,
    idempotency: Idempotency = Idempotency.NATURAL,
    side_effect: SideEffect = SideEffect.NONE,
    readonly: bool = True,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1",
        args_schema={"type": "object", "properties": {}},
        side_effect=side_effect,
        idempotency=idempotency,
        readonly=readonly,
        observations=("ok",),
        timeout_s=5.0,
    )


def _counting_probe(rig: Rig, *, spec: ToolSpec | None = None) -> list[str]:
    """注册（或覆盖）一个计数工具，返回它每次被调用时看到的 `target`。"""
    calls: list[str] = []

    def fn(args, workspace, timeout):
        calls.append(str(args.get("target", "")))
        return ToolResult(ok=True)

    rig.registry.add(spec or _probe_spec(), fn)
    return calls


def _counting_shell(rig: Rig) -> list[str]:
    """覆盖内置 `shell.exec`：真正的 NONE/不可逆声明，但执行换成计数（不跑子进程）。"""
    calls: list[str] = []

    def fn(args, workspace, timeout):
        calls.append(str(args.get("cmd", "")))
        return ToolResult(ok=True, exit_code=0)

    spec = ToolSpec(
        name="shell.exec",
        version="1",
        args_schema={"type": "object"},
        side_effect=SideEffect.IRREVERSIBLE,
        idempotency=Idempotency.NONE,
        readonly=False,
        observations=("ok", "exit_code"),
        timeout_s=5.0,
    )
    rig.registry.add(spec, fn)
    return calls


def _freeze(rig: Rig, plan: Plan, *, run_id: str = "run-recovery") -> str:
    """手工冻结一个 run（和生产 `Runner.start_and_run` 写出同样形状的事件）。"""
    rig.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=run_id,
    )
    rig.writer.emit(
        EventType.PLAN_FROZEN,
        {
            "plan": plan_to_dict(plan),
            "plan_hash": plan.hash(),
            "version": 1,
            "env_fp": snapshot_workspace(rig.ws).fp_hash,
            "probe_fps": {
                step.id: probe_fingerprints(rig.ws, step.env_probes)
                for step in plan.steps
                if step.env_probes
            },
        },
        run_id=run_id,
    )
    return run_id


def _crash_after_start(owner, run_id: str, step_id: str, *, key: str = "k-crash") -> None:
    """把现场做成「状态机到了 running、`step.started` 已落盘、`step.finished` 没落盘」。

    `owner` 是 Rig 或 wiring.App——两者都只有 `writer` 这一件事要借。
    """
    writer = owner.writer
    writer.step_to(run_id, step_id, "running", "crash_window")
    writer.emit(
        EventType.STEP_STARTED,
        {"step_id": step_id, "attempt": 1, "idempotency_key": key},
        run_id=run_id,
        step_id=step_id,
    )


def _attempts(rig: Rig, run_id: str, step_id: str) -> int:
    return rig.world.runs[run_id].steps[step_id].attempts


# --------------------------------------------------------------------------- #
# 1. 冻结 → 跑一半 → 不 resume，直接重放，状态一致
# --------------------------------------------------------------------------- #

def test_replay_after_half_run_matches_the_live_world(rig):
    (rig.ws / "notes.txt").write_text("hello", encoding="utf-8")
    plan = Plan(steps=(
        Step(
            id="read", tool="fs.read", args={"path": "notes.txt"},
            verify=Verify("len(content) > 0", needs=("content",)),
        ),
        Step(
            id="deploy", tool="fs.write", args={"path": "out.txt", "content": "x"},
            depends_on=("read",), gated=True, verify=Verify("ok", needs=("ok",)),
        ),
    ))
    runner = rig.runner()

    started = runner.start_and_run(plan)
    assert started.status == "waiting_human", "gated 步骤把人挡在里面，run 停在半路"

    frozen = rig.of_type(EventType.PLAN_FROZEN, started.run_id)[-1]
    assert frozen.payload["env_fp"], "冻结时必须把环境钉住（env_fp）"

    live = rig.world.fingerprint()
    live_steps = {
        sid: (state.status, state.attempts)
        for sid, state in rig.world.runs[started.run_id].steps.items()
    }
    live_token = rig.world.runs[started.run_id].approval["token"]

    # 「隔天回来」：不 resume，只从事件日志重建
    rig.reload()
    replayed = rig.runner()

    assert rig.world.fingerprint() == live
    assert {
        sid: (state.status, state.attempts)
        for sid, state in rig.world.runs[started.run_id].steps.items()
    } == live_steps
    assert rig.world.runs[started.run_id].status == "waiting_human"
    assert rig.world.runs[started.run_id].approval["token"] == live_token, "挂起事实可重放"
    assert rig.world.runs[started.run_id].env_fp == frozen.payload["env_fp"]
    assert replayed is not None


# --------------------------------------------------------------------------- #
# 2. 死在 step.started 之后 + shell.exec（NONE）→ 转人工，不重跑
# --------------------------------------------------------------------------- #

def test_in_doubt_shell_exec_goes_to_human_instead_of_rerunning(rig):
    calls = _counting_shell(rig)
    plan = Plan(steps=(
        Step(
            id="deploy", tool="shell.exec", args={"cmd": "rm -rf dist"},
            verify=Verify("exit_code == 0", needs=("exit_code",)),
        ),
    ))
    run_id = _freeze(rig, plan)
    _crash_after_start(rig, run_id, "deploy", key="k-unique")

    rig.reload()
    outcome = rig.runner().resume(run_id)

    assert outcome.status == "waiting_human"
    assert calls == [], "不可幂等的步骤不许自动重跑（可能已经执行过）"

    asked = rig.of_type(EventType.APPROVAL_REQUESTED, run_id)
    assert len(asked) == 1
    assert asked[0].payload["step_id"] == "deploy"
    context = asked[0].payload["context"]
    assert "可能已经执行过" in context and "无法确认" in context
    assert "k-unique" in context, "把上一次的幂等键摆到人面前，人才判断得了"

    assert rig.world.runs[run_id].steps["deploy"].status == "running", (
        "in-doubt 的事实不许被抹平：没决定之前它既不是 failed 也不是 pending"
    )
    assert in_doubt_steps(rig.world.runs[run_id]) == ["deploy"]


def test_in_doubt_escalation_can_be_approved_and_then_runs(rig):
    calls = _counting_shell(rig)
    plan = Plan(steps=(
        Step(
            id="deploy", tool="shell.exec", args={"cmd": "rm -rf dist"},
            verify=Verify("exit_code == 0", needs=("exit_code",)),
        ),
    ))
    run_id = _freeze(rig, plan)
    _crash_after_start(rig, run_id, "deploy", key="k-unique")
    rig.reload()
    runner = rig.runner()

    first = runner.resume(run_id)
    assert first.status == "waiting_human"
    token = rig.world.runs[run_id].approval["token"]

    assert rig.human.resolve(run_id, token, "approve") is True
    resumed = runner.resume(run_id, approved_steps=frozenset({"deploy"}))

    assert resumed.status == "completed"
    assert calls == ["rm -rf dist"], "人说了继续，才允许执行"
    assert rig.world.runs[run_id].steps["deploy"].attempts == 2


# --------------------------------------------------------------------------- #
# 3. 死在 step.started 之后 + fs.read（NATURAL）→ 自动重跑，复用同一个幂等键
# --------------------------------------------------------------------------- #

def test_in_doubt_natural_step_retries_with_the_same_idempotency_key(rig):
    (rig.ws / "notes.txt").write_text("hello", encoding="utf-8")
    calls: list[int] = []

    def fn(args, workspace, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise Crash("进程死在读文件的中途")
        return ToolResult(ok=True, data={"content": (rig.ws / "notes.txt").read_text(encoding="utf-8")})

    rig.registry.add(
        _probe_spec("fs.read", idempotency=Idempotency.NATURAL, readonly=True), fn
    )
    plan = Plan(steps=(
        Step(
            id="read", tool="fs.read", args={"path": "notes.txt"},
            verify=Verify("len(content) > 0", needs=("content",)),
        ),
    ))

    runner = rig.runner()
    with pytest.raises(Crash):
        runner.start_and_run(plan)
    run_id = next(iter(rig.world.runs))
    assert rig.world.runs[run_id].steps["read"].status == "running", "崩溃现场停在 running"

    rig.reload()
    outcome = rig.runner().resume(run_id)

    assert outcome.status == "completed"
    assert len(calls) == 2, "读类可以安全重跑：一次崩、一次成"

    started = rig.of_type(EventType.STEP_STARTED, run_id)
    assert [event.payload["attempt"] for event in started] == [1, 2]
    keys = [event.payload["idempotency_key"] for event in started]
    assert keys[0] == keys[1], "重试必须复用同一个幂等键（args 没变，键就该一样）"

    invalidated = rig.of_type(EventType.STEP_INVALIDATED, run_id)
    assert [event.payload["reason"] for event in invalidated] == [RETRY_NATURAL]
    assert rig.world.runs[run_id].steps["read"].status == "passed"


def test_in_doubt_keyed_step_is_requeued_with_the_keyed_reason(rig):
    calls: list[str] = []

    def fn(args, workspace, timeout):
        calls.append(str(args.get("target", "")))
        if len(calls) == 1:
            raise Crash("进程死在扣款请求中途")
        return ToolResult(ok=True)

    rig.registry.add(_probe_spec(idempotency=Idempotency.KEYED), fn)
    plan = Plan(steps=(
        Step(id="charge", tool="probe", args={"target": "order-1"}, verify=Verify("ok", needs=("ok",))),
    ))

    with pytest.raises(Crash):
        rig.runner().start_and_run(plan)
    run_id = next(iter(rig.world.runs))
    first = last_attempt(rig.world.runs[run_id], "charge")
    assert first is not None and first["attempt"] == 1 and first["finished_seq"] == 0
    assert first["started_seq"] > 0

    rig.reload()
    outcome = rig.runner().resume(run_id)

    assert outcome.status == "completed"
    assert calls == ["order-1", "order-1"], "KEYED 会重跑，但带着同一个 key"
    assert [e.payload["reason"] for e in rig.of_type(EventType.STEP_INVALIDATED, run_id)] == [
        RETRY_KEYED
    ]
    keys = [e.payload["idempotency_key"] for e in rig.of_type(EventType.STEP_STARTED, run_id)]
    assert len(keys) == 2
    assert keys[0] == keys[1] == first["idempotency_key"], "KEYED 重跑必须复用同一个幂等键"



# --------------------------------------------------------------------------- #
# 4/5. 环境漂移：变了只失效受影响的步骤；没变什么都不失效
# --------------------------------------------------------------------------- #

def _drift_plan() -> Plan:
    """read 读 workspace 文件（受影响）；audit 是只读探针（不受影响）；publish 等人点头。"""
    return Plan(steps=(
        Step(
            id="read", tool="fs.read", args={"path": "notes.txt"},
            verify=Verify("len(content) > 0", needs=("content",)),
        ),
        Step(
            id="audit", tool="probe", args={"target": "audit"},
            verify=Verify("ok", needs=("ok",)),
        ),
        Step(
            id="publish", tool="probe", args={"target": "publish"},
            depends_on=("read", "audit"), gated=True, verify=Verify("ok", needs=("ok",)),
        ),
    ))


def _suspend_then_approve_tail(rig: Rig) -> tuple[str, Runner, list[str]]:
    """跑一半（read/audit 过、publish 挂起），然后批准 publish 但先不 resume。"""
    (rig.ws / "notes.txt").write_text("v1", encoding="utf-8")
    calls = _counting_probe(rig)  # audit / publish 都走它
    runner = rig.runner()
    started = runner.start_and_run(_drift_plan())
    assert started.status == "waiting_human"
    run_id = started.run_id
    assert _attempts(rig, run_id, "read") == 1
    assert _attempts(rig, run_id, "audit") == 1

    token = rig.world.runs[run_id].approval["token"]
    assert rig.human.resolve(run_id, token, "approve") is True
    return run_id, runner, calls


def test_workspace_drift_invalidates_only_the_affected_passed_step(rig):
    run_id, runner, calls = _suspend_then_approve_tail(rig)

    # 环境变了：workspace 里的输入文件被改
    (rig.ws / "notes.txt").write_text("v2-changed", encoding="utf-8")

    outcome = runner.resume(run_id, approved_steps=frozenset({"publish"}))

    invalidated = rig.of_type(EventType.STEP_INVALIDATED, run_id)
    assert [(event.payload["step_id"], event.payload["reason"]) for event in invalidated] == [
        ("read", "drift")
    ], "只失效产物来自 workspace 的已通过步骤"

    epoch = rig.of_type(EventType.ENV_FINGERPRINTED, run_id)
    assert len(epoch) == 1
    assert epoch[0].payload["changed"] is True
    assert epoch[0].payload["invalidated"] == ["read"]
    assert epoch[0].payload["epoch"] == 1

    assert _attempts(rig, run_id, "read") == 2, "受影响的步骤重跑"
    assert _attempts(rig, run_id, "audit") == 1, "不受影响的已通过步骤不重跑"
    assert calls.count("audit") == 1

    assert outcome.status == "completed"
    read_obs = rig.world.runs[run_id].steps["read"].observations
    assert read_obs["content"] == "v2-changed", "重跑读到的是新环境"


def test_drift_uses_the_declared_env_probes_per_step(rig):
    """`env_probes` 声明的是"这一步只依赖这个文件"：改别的文件不该把它打回重跑。"""
    (rig.ws / "input.txt").write_text("v1", encoding="utf-8")
    (rig.ws / "other.txt").write_text("v1", encoding="utf-8")
    calls = _counting_probe(rig)
    plan = Plan(steps=(
        Step(id="x", tool="probe", args={"target": "x"}, env_probes=("input.txt",),
             verify=Verify("ok", needs=("ok",))),
        Step(id="y", tool="probe", args={"target": "y"}, env_probes=("other.txt",),
             verify=Verify("ok", needs=("ok",))),
        Step(id="gate", tool="probe", args={"target": "gate"}, depends_on=("x", "y"),
             gated=True, verify=Verify("ok", needs=("ok",))),
    ))
    runner = rig.runner()
    started = runner.start_and_run(plan)
    run_id = started.run_id
    assert started.status == "waiting_human"

    frozen = rig.of_type(EventType.PLAN_FROZEN, run_id)[-1].payload
    assert set(frozen["probe_fps"]) == {"x", "y"}, "冻结要逐步骤记下 probe 指纹"
    assert set(frozen["probe_fps"]["x"]) == {"input.txt"}

    # 只动 x 探测的文件
    (rig.ws / "input.txt").write_text("v2", encoding="utf-8")
    token = rig.world.runs[run_id].approval["token"]
    assert rig.human.resolve(run_id, token, "approve") is True

    outcome = runner.resume(run_id, approved_steps=frozenset({"gate"}))

    assert outcome.status == "completed"
    assert calls.count("x") == 2, "自己探测的文件变了 → 必须重跑"
    assert calls.count("y") == 1, "y 的探针没变，却被连带重跑（失效不是按 env_probes 判的）"

    invalidated = rig.of_type(EventType.STEP_INVALIDATED, run_id)
    assert [(e.payload["step_id"], e.payload["reason"]) for e in invalidated] == [("x", "drift")]
    assert invalidated[0].payload["detail"] == {"changed_paths": ["input.txt"]}, (
        "失效要能说清是哪一项变了（detail.changed_paths）"
    )
    epoch = rig.of_type(EventType.ENV_FINGERPRINTED, run_id)[-1]
    assert epoch.payload["probe_drift"] == {"x": ["input.txt"]}


def test_unchanged_workspace_invalidates_nothing(rig):
    run_id, runner, calls = _suspend_then_approve_tail(rig)

    outcome = runner.resume(run_id, approved_steps=frozenset({"publish"}))

    assert rig.of_type(EventType.STEP_INVALIDATED, run_id) == []
    epoch = rig.of_type(EventType.ENV_FINGERPRINTED, run_id)
    assert len(epoch) == 1 and epoch[0].payload["changed"] is False
    assert epoch[0].payload["invalidated"] == []
    assert _attempts(rig, run_id, "read") == 1, "passed 的步骤 attempts 不许涨"
    assert _attempts(rig, run_id, "audit") == 1
    assert outcome.status == "completed"


# --------------------------------------------------------------------------- #
# 6/7. payload_hash 执行前复核
# --------------------------------------------------------------------------- #

def _gated_run(rig: Rig) -> tuple[str, Runner, list[str], str]:
    calls = _counting_probe(rig, spec=_probe_spec(side_effect=SideEffect.REVERSIBLE))
    plan = Plan(steps=(
        Step(
            id="deploy", tool="probe", args={"target": "staging"},
            gated=True, verify=Verify("ok", needs=("ok",)),
        ),
    ))
    runner = rig.runner()
    started = runner.start_and_run(plan)
    assert started.status == "waiting_human"
    token = rig.world.runs[started.run_id].approval["token"]
    assert rig.human.resolve(started.run_id, token, "approve") is True
    return started.run_id, runner, calls, token


def test_tampered_args_after_approval_are_not_executed(rig):
    run_id, runner, calls, token = _gated_run(rig)

    # 批准的是 staging；批准之后计划被改成 prod（§4.7 的「批准 A、执行 B」）
    current = rig.world.runs[run_id].plan
    tampered = dataclasses.replace(current.steps[0], args={"target": "prod"})
    tampered_plan = dataclasses.replace(current, steps=(tampered,), version=current.version + 1)
    rig.writer.emit(
        EventType.PLAN_AMENDED,
        {
            "plan": plan_to_dict(tampered_plan),
            "plan_hash": tampered_plan.hash(),
            "version": tampered_plan.version,
            "reason": "replan_after_approval",
            "invalidated": [],
        },
        run_id=run_id,
    )

    outcome = runner.resume(run_id, approved_steps=frozenset({"deploy"}))

    assert outcome.status == "waiting_human"
    assert calls == [], "批准的是 A、执行的是 B —— 不许执行"

    resolved = rig.of_type(EventType.APPROVAL_RESOLVED, run_id)
    assert [(event.payload["applied"], event.payload.get("reason")) for event in resolved] == [
        (True, None),
        (False, "payload_changed"),
    ]
    asked = rig.of_type(EventType.APPROVAL_REQUESTED, run_id)
    assert len(asked) == 2, "参数变了要重新走一次审批"
    assert asked[-1].payload["token"] != token, "新 token：旧的那次批准已经作废"
    assert asked[-1].payload["payload_hash"] == digest_of(
        {"tool": "probe", "args": {"target": "prod"}}
    )
    assert rig.world.runs[run_id].approval["token"] == asked[-1].payload["token"]


def test_untampered_args_after_approval_execute_normally(rig):
    run_id, runner, calls, _ = _gated_run(rig)

    outcome = runner.resume(run_id, approved_steps=frozenset({"deploy"}))

    assert outcome.status == "completed"
    assert calls == ["staging"], "参数没变就是批准的那一次，正常执行"
    assert rig.of_type(EventType.STEP_STARTED, run_id)[0].payload["attempt"] == 1
    assert rig.of_type(EventType.APPROVAL_RESOLVED, run_id)[-1].payload["applied"] is True


# --------------------------------------------------------------------------- #
# 8. 连续两次 resume：第二次是幂等 no-op
# --------------------------------------------------------------------------- #

def test_second_resume_is_an_idempotent_noop(rig):
    calls = _counting_shell(rig)
    plan = Plan(steps=(
        Step(
            id="deploy", tool="shell.exec", args={"cmd": "deploy.sh"},
            verify=Verify("exit_code == 0", needs=("exit_code",)),
        ),
    ))
    run_id = _freeze(rig, plan)
    _crash_after_start(rig, run_id, "deploy", key="k-idem")

    rig.reload()
    runner = rig.runner()

    first = runner.resume(run_id)
    assert first.status == "waiting_human"
    token = rig.world.runs[run_id].approval["token"]
    before = len(rig.events(run_id))

    second = runner.resume(run_id)

    assert second.status == "waiting_human"
    assert len(rig.events(run_id)) == before, "第二次 resume 一条事件都不该多写"
    assert len(rig.of_type(EventType.APPROVAL_REQUESTED, run_id)) == 1, "不许重复发 token"
    assert rig.world.runs[run_id].approval["token"] == token
    assert rig.of_type(EventType.RUN_RESUMED, run_id) == [], "还没真正续跑，就不该有 run.resumed"
    assert calls == []


# --------------------------------------------------------------------------- #
# 对账模块本身：纯函数部分（不写事件也能分类）
# --------------------------------------------------------------------------- #

def test_reconcile_classifies_without_writing_when_no_writer(rig):
    plan = Plan(steps=(
        Step(id="read", tool="fs.read", args={"path": "notes.txt"}),
        Step(id="deploy", tool="shell.exec", args={"cmd": "rm -rf dist"}),
    ))
    run_id = _freeze(rig, plan)
    _crash_after_start(rig, run_id, "read", key="k-read")
    _crash_after_start(rig, run_id, "deploy", key="k-deploy")
    run = rig.world.runs[run_id]
    before = len(rig.events(run_id))

    ask = reconcile_in_doubt(run, rig.registry)

    assert ask == ["deploy"], "NATURAL 不用问人；NONE 要问"
    assert len(rig.events(run_id)) == before, "不给 writer 就不写事件（dry-run）"
    assert in_doubt_steps(run) == ["read", "deploy"]
    assert last_attempt(run, "read")["idempotency_key"] == "k-read"
    assert last_attempt(run, "publish") is None, "从没开始过的步骤没有 attempt"


# --------------------------------------------------------------------------- #
# 7. 崩溃窗口：工具跑完了，但判定/状态没走完
# --------------------------------------------------------------------------- #

def _finished_run(rig: Rig, *, observation_ok: bool, verify_after: bool) -> tuple[str, list[str]]:
    """手工搭出「step.started + step.finished（+ 可选 step.verified）」但状态仍是 running。"""
    calls = _counting_probe(rig)
    plan = Plan(steps=(
        Step(
            id="probe", tool="probe", args={"target": "t"},
            verify=Verify("ok == True", needs=("ok",)),
        ),
    ))
    run_id = _freeze(rig, plan)
    rig.writer.step_to(run_id, "probe", "running", "crash_window")
    rig.writer.emit(
        EventType.STEP_STARTED,
        {"step_id": "probe", "attempt": 1, "idempotency_key": "k-done"},
        run_id=run_id, step_id="probe",
    )
    rig.writer.emit(
        EventType.STEP_FINISHED,
        {
            "step_id": "probe", "status": "ok", "artifact_refs": [],
            "observations": {"ok": observation_ok}, "attempt": 1,
        },
        run_id=run_id, step_id="probe",
    )
    if verify_after:
        rig.writer.emit(
            EventType.STEP_VERIFIED,
            {
                "step_id": "probe", "result": "pass" if observation_ok else "fail",
                "checks": [{"expr": "ok == True", "passed": observation_ok}], "reason": "",
            },
            run_id=run_id, step_id="probe",
        )
    return run_id, calls


def test_crash_between_finished_and_verified_reverifies_without_rerunning(rig):
    run_id, calls = _finished_run(rig, observation_ok=True, verify_after=False)
    assert rig.world.runs[run_id].steps["probe"].status == "running"

    rig.reload()
    outcome = rig.runner().resume(run_id)

    assert outcome.status == "completed", f"只差判定的步骤不该把 run 判死：{outcome.reason}"
    assert calls == [], "工具已经跑完了，恢复只重放断言，不许重跑工具"
    assert len(rig.of_type(EventType.STEP_STARTED, run_id)) == 1
    assert len(rig.of_type(EventType.STEP_VERIFIED, run_id)) == 1
    assert rig.world.runs[run_id].steps["probe"].status == "passed"


def test_crash_between_verified_and_state_change_completes_from_the_log(rig):
    run_id, calls = _finished_run(rig, observation_ok=True, verify_after=True)

    rig.reload()
    outcome = rig.runner().resume(run_id)

    assert outcome.status == "completed"
    assert calls == []
    assert len(rig.of_type(EventType.STEP_VERIFIED, run_id)) == 1, (
        "判定已经落盘，恢复不该再求值一次（也不该写第二条 step.verified）"
    )
    assert rig.world.runs[run_id].steps["probe"].status == "passed"


def test_recovered_step_failing_its_assertion_ends_the_run_honestly(rig):
    run_id, calls = _finished_run(rig, observation_ok=False, verify_after=False)

    rig.reload()
    outcome = rig.runner().resume(run_id)

    assert calls == [], "断言挂了也不许偷偷重跑工具"
    assert outcome.status == "failed"
    assert outcome.reason == "assertion:probe", (
        f"失败原因必须是断言，而不是 unfinished：{outcome.reason}"
    )
    assert rig.world.runs[run_id].steps["probe"].status == "failed"
    classes = [e.payload["class"] for e in rig.of_type(EventType.FAILURE_CLASSIFIED, run_id)]
    assert classes == ["assertion"]


# --------------------------------------------------------------------------- #
# 指纹本身：文件太多时退化成「目录 + 大小」，且跳过目录真的被跳过
# --------------------------------------------------------------------------- #

def test_workspace_snapshot_falls_back_to_dir_rollup(tmp_path):
    ws = tmp_path / "tree"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.txt").write_text("1", encoding="utf-8")
    (ws / "sub" / "b.txt").write_text("2", encoding="utf-8")

    full = snapshot_workspace(ws)
    assert (full.mode, full.files, full.truncated) == ("files", 2, False)

    capped = snapshot_workspace(ws, max_files=1)
    assert (capped.mode, capped.truncated) == ("dirs+size", True)
    assert capped.fp_hash != full.fp_hash

    (ws / "sub" / "b.txt").write_text("22", encoding="utf-8")
    assert snapshot_workspace(ws, max_files=1).fp_hash != capped.fp_hash, "大小变了也看得见"

    noise = ws / ".git"
    noise.mkdir()
    (noise / "HEAD").write_text("ref: main", encoding="utf-8")
    assert snapshot_workspace(ws).files == 2, ".git 不是输入，不参与指纹"


# --------------------------------------------------------------------------- #
# 人拒绝一个 in-doubt 步骤：必须合法地停下来（不是 illegal transition 崩掉）
# --------------------------------------------------------------------------- #

def test_rejecting_an_in_doubt_step_stops_the_run_legally(tmp_path):
    from jagent.wiring import build_app

    app = build_app(tmp_path / "app")
    calls: list[str] = []

    def fn(args, workspace, timeout):
        calls.append(str(args.get("cmd", "")))
        return ToolResult(ok=True, exit_code=0)

    app.registry.add(
        ToolSpec(
            name="shell.exec", version="1", args_schema={"type": "object"},
            side_effect=SideEffect.IRREVERSIBLE, idempotency=Idempotency.NONE,
            readonly=False, observations=("ok", "exit_code"), timeout_s=5.0,
        ),
        fn,
    )
    try:
        run_id = "run-in-doubt"
        plan = Plan(steps=(
            Step(
                id="deploy", tool="shell.exec", args={"cmd": "deploy.sh"},
                verify=Verify("exit_code == 0", needs=("exit_code",)),
            ),
        ))
        app.writer.emit(
            EventType.RUN_STARTED,
            {"run_id": run_id, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
            run_id=run_id,
        )
        app.writer.emit(
            EventType.PLAN_FROZEN,
            {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1, "env_fp": ""},
            run_id=run_id,
        )
        _crash_after_start(app, run_id, "deploy", key="k-human")

        assert app.runner.resume(run_id).status == "waiting_human"
        assert app.world.runs[run_id].approval["step_id"] == "deploy"

        outcome = app.session.resume_run(run_id, "reject")

        assert outcome.status == "answered"
        run = app.world.runs[run_id]
        assert run.status == "failed"
        assert run.fail_reason == "rejected_by_human"
        assert run.steps["deploy"].status == "skipped"
        assert run.approval is None
        assert calls == [], "拒绝就是不做，不许偷偷执行"
        assert [
            event.payload["reason"]
            for event in app.store.read(run_id=run_id)
            if event.type is EventType.STEP_INVALIDATED
        ] == ["rejected_in_doubt"]
    finally:
        app.close()

