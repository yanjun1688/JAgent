"""JAgent v8 · 端口（Protocol）。

这些是 `agent/` 与 `harness/` 之间**唯一**的接触面。
测试用 `Scripted*` 实现它们，生产用 LLM 实现它们，双方互不知道对方存在。
（模板场景层已删除：计划 / 建议一律由模型起草、由代码校验。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .contracts import (
    Advice,
    EvidenceBundle,
    EvidenceReq,
    GateVerdict,
    IntentDecision,
    Plan,
    PlannerDraft,
    Slot,
)


@dataclass
class ModelReply:
    """一次模型调用的结果。text 是原始输出，解析交给调用方。"""

    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ModelUnavailable(RuntimeError):  # noqa: N818 - 名字按语义取，不按 lint 后缀
    """没有接模型，或当前处于重放模式（重放期间禁止任何模型调用）。"""


class ModelPort(Protocol):
    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply: ...


class NoModel:
    """没接模型：任何调用都立刻失败。默认装配就是它（模板/规则驱动）。"""

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0) -> ModelReply:
        raise ModelUnavailable("no model configured")


class RouterPort(Protocol):
    def route(
        self,
        *,
        text: str,
        slots: dict[str, Slot],
        turns: Sequence[str],
        memory: Sequence[str] = (),
    ) -> IntentDecision: ...


class PlannerPort(Protocol):
    def plan(
        self,
        *,
        decision: IntentDecision,
        slots: dict[str, Slot],
        tools: list[dict[str, Any]],
        text: str = "",
        replan_of: Plan | None = None,
        failure: dict[str, Any] | None = None,
        turns: Sequence[str] = (),
        memory: Sequence[str] = (),
    ) -> PlannerDraft: ...


class RepairerPort(Protocol):
    def repair(
        self,
        *,
        plan: Plan,
        step_id: str,
        failure: dict[str, Any],
        attempt: int,
    ) -> Plan | None: ...


class AdviceGatePort(Protocol):
    """建议侧合规校验端口。实现方（gate/）不依赖 agent/。"""

    def check(
        self,
        advice: Advice,
        evidence: EvidenceBundle | None = None,
        plan: Plan | None = None,
    ) -> GateVerdict: ...


class ProposePort(Protocol):
    """gather 收尾的建议合成端口；与 Planner.plan 是两条路径（D-004）。"""

    def compose(
        self,
        *,
        slots: dict[str, Slot],
        text: str,
        evidence: EvidenceBundle,
        missing: tuple[EvidenceReq, ...],
        then: str | None,
    ) -> Advice: ...
