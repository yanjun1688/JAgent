"""v8 加固验收 · 共用夹具。

独立验收：断言只来自需求（G-05 / G-06），不参考实现方的测试，不 monkeypatch
被测代码。夹具只装配公开契约（contracts / store / fold / writer / registry /
executor / runner），"执行了几次" 这类事实一律用工具调用计数与事件流来数。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jagent.app.runner import Runner
from jagent.contracts import (
    Budget,
    EventType,
    Idempotency,
    Plan,
    SideEffect,
    ToolResult,
    ToolSpec,
    canonical_json,
    plan_to_dict,
)
from jagent.harness.artifacts import ArtifactStore
from jagent.harness.executor import Executor
from jagent.harness.fold import World
from jagent.harness.human import HumanCenter
from jagent.harness.registry import ToolRegistry, build_default_registry
from jagent.harness.repair import NoopRepairer
from jagent.harness.report import Reporter
from jagent.harness.store import EventStore
from jagent.harness.verify import Verifier
from jagent.harness.writer import Writer

SESSION_ID = "s_hardening"


@dataclass
class Bench:
    """一个独立的事件底座 + 空 workspace。"""

    root: Path
    ws: Path
    store: EventStore
    world: World
    writer: Writer
    registry: ToolRegistry
    executor: Executor
    human: HumanCenter

    # ---------------------------------------------------------------- 事件 --
    def events(self, *, run_id: str | None = None):
        return self.store.read(run_id=run_id)

    def of_type(self, event_type: EventType, *, run_id: str | None = None):
        return [e for e in self.events(run_id=run_id) if e.type is event_type]

    # ---------------------------------------------------------------- 工具 --
    def add_tool(
        self,
        name: str = "probe",
        *,
        side_effect: SideEffect = SideEffect.NONE,
        idempotency: Idempotency = Idempotency.NATURAL,
        data: dict[str, Any] | None = None,
        fail_times: int = 0,
        error_code: str = "timeout",
    ) -> list[dict[str, Any]]:
        """注册一个计数工具，返回调用参数列表（长度就是执行次数）。"""
        calls: list[dict[str, Any]] = []
        payload = dict(data or {})

        def fn(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
            calls.append(dict(args))
            if len(calls) > 30:  # 恢复逻辑真死循环时让测试失败，而不是挂住
                raise AssertionError(f"tool {name!r} called {len(calls)} times; recovery loops")
            if len(calls) <= fail_times:
                return ToolResult(
                    ok=False, error_code=error_code, error_message="injected failure"
                )
            return ToolResult(ok=True, data=dict(payload))

        self.registry.add(
            ToolSpec(
                name=name,
                version="1",
                args_schema={"type": "object"},
                side_effect=side_effect,
                idempotency=idempotency,
                observations=("ok",),
                timeout_s=5.0,
            ),
            fn,
        )
        return calls


def _build(tmp_path: Path, *, real_shell: bool) -> Bench:
    ws = tmp_path / "ws"
    ws.mkdir()
    store = EventStore(tmp_path / "events.db")
    world = World()
    writer = Writer(store, world, SESSION_ID)
    registry = build_default_registry(ws) if real_shell else ToolRegistry()
    executor = Executor(registry, ArtifactStore(tmp_path / "artifacts"), writer, workspace=ws)
    return Bench(
        root=tmp_path,
        ws=ws,
        store=store,
        world=world,
        writer=writer,
        registry=registry,
        executor=executor,
        human=HumanCenter(writer),
    )


@pytest.fixture()
def bench(tmp_path: Path):
    """假工具底座：G-05 用（调用计数就是事实）。"""
    item = _build(tmp_path, real_shell=False)
    yield item
    item.store.close()


@pytest.fixture()
def shell_bench(tmp_path: Path):
    """真 registry（含真 shell.exec）：G-06 用，执行范围限制在 tmp workspace。"""
    item = _build(tmp_path, real_shell=True)
    yield item
    item.store.close()


# --------------------------------------------------------------------------- #
# Runner 工厂
# --------------------------------------------------------------------------- #

def make_runner(
    bench: Bench,
    *,
    auto_approve: frozenset[str] | set[str] = frozenset(),
    budget: Budget | None = None,
    repairer: Any = None,
    verifier: Any = None,
) -> Runner:
    return Runner(
        writer=bench.writer,
        executor=bench.executor,
        verifier=verifier or Verifier(),
        repairer=repairer or NoopRepairer(),
        reporter=Reporter(),
        human=bench.human,
        budget=budget or Budget(),
        registry=bench.registry,
        auto_approve=frozenset(auto_approve),
    )


# --------------------------------------------------------------------------- #
# 事件级辅助
# --------------------------------------------------------------------------- #

def start_run(bench: Bench, run_id: str, plan: Plan) -> None:
    """写 RUN_STARTED + PLAN_FROZEN，不驱动执行（崩溃态需要手工搭建）。

    payload 形状必须与生产冻结一致（`plan` / `plan_hash` / `version` / `env_fp`），
    否则造出来的是生产日志里永远不会出现的状态。指纹从这里取，才能让对账逻辑
    真的看到"冻结时环境长什么样"。
    """
    env_fp = ""
    try:
        from jagent.harness.fingerprint import snapshot_workspace

        env_fp = snapshot_workspace(bench.ws).fp_hash
    except Exception:  # pragma: no cover - 指纹模块被改名/删掉时退化成"没有冻结指纹"
        env_fp = ""
    bench.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=run_id,
    )
    bench.writer.emit(
        EventType.PLAN_FROZEN,
        {
            "plan": plan_to_dict(plan),
            "plan_hash": plan.hash(),
            "version": plan.version,
            "env_fp": env_fp,
        },
        run_id=run_id,
    )


def frozen_event(bench: Bench, run_id: str):
    events = bench.of_type(EventType.PLAN_FROZEN, run_id=run_id)
    assert events, f"run {run_id} 没有 plan.frozen"
    return events[-1]


#: 实现方给"环境指纹"起什么名字都可以，但不能没有。这里按常见写法取值。
_ENV_KEYS = (
    "env_fingerprint",
    "environment_fingerprint",
    "env_hash",
    "env_fp",
    "fp_hash",
    "fingerprint",
    "environment",
    "env",
)

#: 独立的"环境指纹"事件（实现方若把指纹写在自己的一条事件里，也认）。
_ENV_EVENT = "env.fingerprinted"


def _pick_fp(payload: dict[str, Any]) -> str | None:
    for key in _ENV_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (dict, list)) and value:
            return canonical_json(value)
    return None


def env_fingerprint(bench: Bench, run_id: str) -> str:
    """取这次 run 在**冻结时**记录的环境指纹。

    指纹放在 `plan.frozen` 的一个字段里、或者单独一条 `env.fingerprinted`
    事件里都算数；取不到就直接失败并点名现有字段。
    """
    frozen = frozen_event(bench, run_id)
    direct = _pick_fp(frozen.payload)
    if direct is not None:
        return direct
    for event in bench.events(run_id=run_id):
        if event.type.value != _ENV_EVENT:
            continue
        found = _pick_fp(event.payload)
        if found is not None:
            return found
    raise AssertionError(
        "plan.frozen（以及同 run 的事件流）没有记录环境指纹：恢复时无法判断环境变过没有。"
        f"plan.frozen 现有字段 {sorted(frozen.payload)}；期望其中有一项"
        f"（{', '.join(_ENV_KEYS)}）或一条 {_ENV_EVENT} 事件，且与 plan_hash 不是同一个值。"
    )


def step_params(step) -> dict[str, Any]:
    from jagent.contracts import step_to_dict

    return step_to_dict(step)
