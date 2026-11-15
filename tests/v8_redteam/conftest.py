"""红队共用夹具：事件底座、可注入的 Runner、可注入的 App。

这里只装配**公开契约**（contracts / ports / statemachine / store / writer），
不复制生产逻辑，也不 monkeypatch 被测代码。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from jagent.app.runner import Runner
from jagent.contracts import (
    Budget,
    Idempotency,
    SideEffect,
    ToolResult,
    ToolSpec,
)
from jagent.harness.artifacts import ArtifactStore
from jagent.harness.executor import Executor
from jagent.harness.fold import World
from jagent.harness.human import HumanCenter
from jagent.harness.registry import ToolRegistry, build_default_registry
from jagent.harness.repair import HeuristicRepairer
from jagent.harness.report import Reporter
from jagent.harness.store import EventStore
from jagent.harness.verify import Verifier
from jagent.harness.writer import Writer
from jagent.wiring import build_app

# --------------------------------------------------------------------------- #
# 让 pyproject 里注册的 known_bug marker 真的等于 strict xfail。
#
# pyproject 只注册了 marker，没有任何 hook 把它接到 pytest 的 xfail 机制上；
# 这里补上。strict=True：缺口被修好后 XPASS 会显式报错，逼人删掉标记。
# --------------------------------------------------------------------------- #

def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = item.get_closest_marker("known_bug")
        if marker is None or item.get_closest_marker("xfail") is not None:
            continue
        reason = str(marker.kwargs.get("reason") or "known bug")
        marker_id = marker.kwargs.get("id")
        if marker_id:
            reason = f"[{marker_id}] {reason}"
        item.add_marker(pytest.mark.xfail(strict=True, reason=reason))


# --------------------------------------------------------------------------- #
# 事件底座
# --------------------------------------------------------------------------- #

@dataclass
class Harness:
    """一个独立的 store + world + writer，带一个空的 workspace。"""

    root: Path
    ws: Path
    store: EventStore
    world: World
    writer: Writer
    db_path: Path
    _closed: bool = field(default=False, repr=False)

    def events(self):
        return self.store.read()

    def of_type(self, event_type):
        return [event for event in self.events() if event.type is event_type]


@pytest.fixture()
def harness(tmp_path: Path):
    store = EventStore(tmp_path / "events.db")
    world = World()
    ws = tmp_path / "ws"
    ws.mkdir()
    item = Harness(
        root=tmp_path,
        ws=ws,
        store=store,
        world=world,
        writer=Writer(store, world, "s_redteam"),
        db_path=tmp_path / "events.db",
    )
    yield item
    item.store.close()


# --------------------------------------------------------------------------- #
# 工具替身
# --------------------------------------------------------------------------- #

def probe_spec(
    name: str = "probe",
    *,
    observations: tuple[str, ...] = ("ok",),
    side_effect: SideEffect = SideEffect.NONE,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1",
        args_schema={"type": "object", "properties": {}},
        side_effect=side_effect,
        idempotency=Idempotency.NATURAL,
        observations=observations,
        timeout_s=5.0,
    )


def probe_tool(
    *,
    name: str = "probe",
    data: dict[str, Any] | None = None,
    ok: bool = True,
    error_code: str | None = None,
    observations: tuple[str, ...] = ("ok",),
    side_effect: SideEffect = SideEffect.NONE,
) -> tuple[ToolSpec, Callable[..., ToolResult], list[dict[str, Any]]]:
    """一个计数工具：返回固定数据，并记录每次调用的 args。"""

    calls: list[dict[str, Any]] = []
    payload = dict(data or {})
    limit = 25

    def fn(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
        calls.append(dict(args))
        # 防止被测调度真的死循环：超过上限直接让整条 run 失败而不是卡住测试。
        if len(calls) > limit:
            raise AssertionError(f"tool {name!r} called more than {limit} times; scheduler loops")
        return ToolResult(ok=ok, data=dict(payload), error_code=error_code)

    return probe_spec(name, observations=observations, side_effect=side_effect), fn, calls


# --------------------------------------------------------------------------- #
# Runner 工厂
# --------------------------------------------------------------------------- #

def make_runner(
    harness: Harness,
    *,
    tools: list[tuple[ToolSpec, Callable[..., ToolResult]]] | None = None,
    registry: ToolRegistry | None = None,
    budget: Budget | None = None,
    verifier: Any = None,
    repairer: Any = None,
    executor: Any = None,
    auto_approve: frozenset[str] = frozenset(),
    human: Any = None,
) -> Runner:
    """按当前 Runner 的公开签名装配，不预先假定可选参数永远存在。"""

    if registry is None:
        registry = ToolRegistry()
        for spec, fn in tools or []:
            registry.add(spec, fn)

    artifacts = ArtifactStore(harness.root / "artifacts")
    if executor is None:
        executor = Executor(registry, artifacts, harness.writer, workspace=harness.ws)

    kwargs: dict[str, Any] = {
        "writer": harness.writer,
        "executor": executor,
        "verifier": verifier if verifier is not None else Verifier(),
        "repairer": repairer if repairer is not None else HeuristicRepairer(),
        "reporter": Reporter(),
        "human": human if human is not None else HumanCenter(harness.writer),
        "budget": budget or Budget(),
    }
    params = inspect.signature(Runner.__init__).parameters
    if "registry" in params:
        kwargs["registry"] = registry
    if "auto_approve" in params:
        kwargs["auto_approve"] = auto_approve
    return Runner(**kwargs)


@pytest.fixture(name="make_runner")
def make_runner_fixture():
    """把上面的工厂以 fixture 形式暴露，测试可直接声明 ``make_runner``。"""

    return make_runner


# --------------------------------------------------------------------------- #
# App 工厂（生产装配路径）
# --------------------------------------------------------------------------- #

@pytest.fixture()
def app_factory(tmp_path: Path):
    created = []

    def make(name: str = "app", **kwargs: Any):
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        app = build_app(root, **kwargs)
        created.append(app)
        return app

    yield make
    for app in created:
        app.close()


__all__ = [
    "Harness",
    "build_default_registry",
    "make_runner",
    "probe_spec",
    "probe_tool",
]
