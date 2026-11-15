"""I1 共用夹具：一个独立的 store + world + writer（不借用 app 层装配）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from jagent.harness.fold import World
from jagent.harness.store import EventStore
from jagent.harness.writer import Writer

SESSION_ID = "s_i1"
RUN_ID = "run_i1"
STEP_ID = "s1"
#: 复现记录里那条产物的 id（64 位 hex，形状与真实 sha256 一致）。
ARTIFACT_ID = "94285136dc8e18e8ba2ef27a03b12c3fc0efcafab0ba63e8f994271b0bb37778"


@dataclass
class Harness:
    root: Path
    store: EventStore
    world: World
    writer: Writer
    db_path: Path

    def events(self):
        return self.store.read()

    def of_type(self, event_type):
        return [event for event in self.events() if event.type is event_type]


@pytest.fixture()
def harness(tmp_path: Path):
    store = EventStore(tmp_path / "events.db")
    world = World()
    item = Harness(
        root=tmp_path,
        store=store,
        world=world,
        # writer 与 world 是同一个对象：live 投影和重放投影才可比。
        writer=Writer(store, world, SESSION_ID),
        db_path=tmp_path / "events.db",
    )
    yield item
    store.close()
