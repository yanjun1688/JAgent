"""tests/v8 共享夹具：最小 runner rig（真 store + 真 fold + 真 Writer）。

recovery_v8 的同名 ``rig`` 更富（带 probe 指纹基线），本地定义遮蔽这里的；
runner_cancel / runner_gaps 用这个共享版。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from jagent.app.runner import Runner
from jagent.contracts import Budget
from jagent.harness.artifacts import ArtifactStore
from jagent.harness.executor import Executor
from jagent.harness.fold import World, fold
from jagent.harness.human import HumanCenter
from jagent.harness.repair import HeuristicRepairer
from jagent.harness.registry import ToolRegistry, build_default_registry
from jagent.harness.report import Reporter
from jagent.harness.store import EventStore
from jagent.harness.verify import Verifier
from jagent.harness.writer import Writer

SESSION_ID = "s-v8-shared"


@dataclass
class Rig:
    root: Path
    ws: Path
    store: EventStore
    writer: Writer
    registry: ToolRegistry
    executor: Executor
    human: HumanCenter

    @property
    def world(self):
        return self.writer.world

    def reload(self):
        """模拟进程重启：只从事件日志重建世界（零模型、零内存残留）。"""
        self.writer.world = fold(self.store.read())
        return self.writer.world

    def runner(self, *, budget: Budget | None = None) -> Runner:
        return Runner(
            writer=self.writer,
            executor=self.executor,
            verifier=Verifier(),
            repairer=HeuristicRepairer(),
            reporter=Reporter(),
            human=self.human,
            budget=budget or Budget(),
            registry=self.registry,
        )

    def events(self) -> list:
        return self.store.read()

    def of_type(self, event_type) -> list:
        return [event for event in self.events() if event.type is event_type]


@pytest.fixture()
def rig(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = EventStore(tmp_path / "events.db")
    writer = Writer(store, World(), SESSION_ID)
    registry = build_default_registry(ws)
    executor = Executor(registry, ArtifactStore(tmp_path / "artifacts"), writer, workspace=ws)
    item = Rig(
        root=tmp_path,
        ws=ws,
        store=store,
        writer=writer,
        registry=registry,
        executor=executor,
        human=HumanCenter(writer),
    )
    yield item
    store.close()
