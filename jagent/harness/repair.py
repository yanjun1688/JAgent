"""JAgent v8 · 修复器（Repairer）。

修复是**受信决策**：只能从失败事实出发，做可确定性验证的改动。
不允许猜——猜出来的新计划会污染 plan_hash，事后无法解释为什么改。

三个实现：
- `NoopRepairer`：什么也不修（默认 / 测试基线）；
- `CallableRepairer`：把决策外包给一个函数（LLM 或脚本），本文件不认识它；
- `HeuristicRepairer`：只认两类确定性的情况，其余一律 `None`。

`None` 的语义是「我修不了」，不是「修好了但没变化」：调度侧据此升级人工
（`OnFail.ASK_USER`）而不是无限重试。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any, Callable

from ..contracts import Plan, Step, digest_of

#: timeout 翻倍的上限（秒）。超过这个值再翻倍只是把失败变慢，不是修复。
MAX_TIMEOUT_S = 120.0


def reuse_key(step: Step, upstream_artifact_refs: Iterable[str]) -> tuple[str, str, tuple[str, ...]]:
    """增量重跑的复用键：``(step_id, args_hash, 上游产物 hash)``。

    纯函数，不读事件、不读世界。语义（架构 §4.10 / D-027）：

    - ``step_id``：换一个 id 就是另一步，不存在"复用"；
    - ``digest_of(step.args)``：args 没变，这次调用的输入就没变；
    - 上游 ``artifact_refs``（排序后）：上游产物按内容寻址，hash 相同即产物相同。

    ``upstream_artifact_refs`` 由调用方按新/旧计划的 ``depends_on`` 从投影里收集；
    这里再排序，保证依赖声明顺序不影响复用判定。本函数只算键，不判定谁重跑：
    "键变了就重跑、键没变就复用、下游是否跟着失效"是 Runner 的策略。
    """

    return (step.id, digest_of(step.args), tuple(sorted(upstream_artifact_refs)))


class NoopRepairer:
    """不修：返回 None，让调度侧按 on_fail 走下一档。"""

    def repair(
        self,
        *,
        plan: Plan,
        step_id: str,
        failure: dict[str, Any],
        attempt: int,
    ) -> Plan | None:
        return None


class CallableRepairer:
    """把修复逻辑原样转给 `fn`；本类只保证端口签名一致。

    `fn` 可以是 LLM planner、脚本、测试替身。返回值不做解释：
    非 None 必须是完整的新 Plan（由被调方负责 step 顺序与依赖仍然自洽）。
    """

    def __init__(self, fn: Callable[..., Plan | None]) -> None:
        self._fn = fn

    def repair(
        self,
        *,
        plan: Plan,
        step_id: str,
        failure: dict[str, Any],
        attempt: int,
    ) -> Plan | None:
        return self._fn(plan=plan, step_id=step_id, failure=failure, attempt=attempt)


class HeuristicRepairer:
    """只处理两类可确定性修复的情况，其余返回 None（不猜）。

    规则（顺序即优先级）：
    1. `failure["reason"]` 含 `evidence_missing` → None。
       缺观测是事实缺口，改计划只会把「看不见」变成「假装看见」。
    2. `failure["class"] == "transient"` 且 step.tool == "shell.exec"
       且 args 里有 timeout_s → timeout_s 翻倍（上限 120），返回新 Plan。
    3. 其它 → None。
    """

    def repair(
        self,
        *,
        plan: Plan,
        step_id: str,
        failure: dict[str, Any],
        attempt: int,
    ) -> Plan | None:
        if not isinstance(failure, dict):
            return None

        # 1) 缺观测不修：先判，且优先级最高（哪怕 class 是 transient）。
        reason = str(failure.get("reason") or "")
        if "evidence_missing" in reason:
            return None

        # 2) 只修一种：瞬时失败 + shell.exec + 显式 timeout_s。
        if failure.get("class") != "transient":
            return None
        step = plan.by_id().get(step_id)
        if step is None or step.tool != "shell.exec":
            return None
        if "timeout_s" not in step.args:
            return None
        try:
            current = float(step.args["timeout_s"])
        except (TypeError, ValueError):
            return None

        timeout_s = min(current * 2.0, MAX_TIMEOUT_S)
        new_step = dataclasses.replace(step, args={**step.args, "timeout_s": timeout_s})

        # 只换这一个 step，其它保持原对象（不重建、不重排、不动 version）。
        steps = tuple(new_step if s.id == step_id else s for s in plan.steps)
        return dataclasses.replace(plan, steps=steps)
