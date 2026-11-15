"""JAgent v8 · 状态机。

两张表，不合并：
- run:   planning → running → (waiting_human ↔ running) → completed / failed / cancelled
- step:  pending → running → passed / failed → (pending) / skipped；passed → stale → pending

`waiting_human` 只属于 run；`stale` 只属于 step。非法转移在**写入侧**拒绝，
拒绝本身也写成事件（`illegal: true`），因为「拒过」也是事实。
"""

from __future__ import annotations

from ..contracts import EventType

RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"running", "failed", "cancelled"}),
    # running / waiting_human 允许自环：审批链（APPROVAL_RECEIVED 把投影置为
    # running，随后的 RUN_RESUMED 是对当前状态的再声明）与挂起中的重复请求
    # （supersede 旧 token，HumanCenter 既有设计）都不是"转移"，是确认。
    # planning 没有自环：重复 run.started 是 bug，要拒（BUG2-02 守卫）。
    "running": frozenset({"waiting_human", "completed", "failed", "cancelled", "running"}),
    "waiting_human": frozenset({"running", "failed", "cancelled", "waiting_human"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "skipped"}),
    "running": frozenset({"passed", "failed"}),
    "failed": frozenset({"pending", "skipped"}),
    "passed": frozenset({"stale"}),
    "stale": frozenset({"pending", "running"}),
    "skipped": frozenset(),
}

RUN_TERMINAL = frozenset({"completed", "failed", "cancelled"})


class IllegalTransitionError(RuntimeError):
    def __init__(self, layer: str, from_: str, to: str) -> None:
        self.layer = layer
        self.from_ = from_
        self.to = to
        super().__init__(f"illegal {layer} transition: {from_} -> {to}")


def run_can(from_: str, to: str) -> bool:
    return to in RUN_TRANSITIONS.get(from_, frozenset())


def step_can(from_: str, to: str) -> bool:
    return to in STEP_TRANSITIONS.get(from_, frozenset())


def ensure_run(from_: str, to: str) -> None:
    if not run_can(from_, to):
        raise IllegalTransitionError("run", from_, to)


def ensure_step(from_: str, to: str) -> None:
    if not step_can(from_, to):
        raise IllegalTransitionError("step", from_, to)


def step_event(step_id: str, from_: str, to: str, reason: str, *, illegal: bool = False) -> EventType:
    return EventType.STEP_STATE_CHANGED
