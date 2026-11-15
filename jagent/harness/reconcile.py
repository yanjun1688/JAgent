"""JAgent v8 · 恢复对账（reconcile）。

「恢复 = 重放 + 对账 + 续跑」（E17）。三件事各有归属：
重放在 `fold`，续跑在 `Runner.drive`，本模块只做中间那一步——把日志里
**说不清**的步骤变成说得清的决策。

三种说不清的情况，三种诚实：

1. **in-doubt**：`step.started` 写了、`step.finished` 没写（进程死在工具调用中途）。
   日志只知道「开始了」，不知道「做没做」。这里不猜，按 `ToolSpec.idempotency` 分流：
   - `NATURAL`（读类）：重复执行不会多出副作用 → 直接重排队（`idempotent_retry`）；
   - `KEYED`：重跑要复用**同一个** `idempotency_key`（`last_attempt` 能取到），
     工具侧按 key 去重 → 也重排队（`keyed_retry`）；
   - `NONE`（`shell.exec` 这种）：可能已经执行过，重跑等于赌第二遍副作用 →
     **不猜**，交给人（`approval.requested` + 「可能已经执行过，无法确认」）。
2. **环境漂移**：环境内容变了。声明了 `step.env_probes` 的步骤**逐项**比它自己的
   探针文件（`probe_drift`），其余步骤才看整树指纹；只失效「已 passed 且真的受影响」
   的步骤（`drift_victims`），并写清是哪一个文件变的。
3. **幂等键**：重试必须复用同一个 key。key 由 `Executor` 按
   `sha256(run_id|step_id|digest_of(args))` 算，args 不变则天然相同；
   `last_attempt` 把上一次的身份从事件里读回来，恢复侧才有东西可对。

还有一类**不是** in-doubt 的崩溃窗口（工具已经跑完，死在判定/状态转移之前）：
`awaiting_verification` / `awaiting_transition` 负责识别，`outcome_from_log` /
`verdict_from_log` 把落盘的事实还原出来交给 `Runner` 重放判定——**绝不重跑工具**。

本模块**不新增事件类型**：只用 `step.invalidated` / `step.state_changed`（`Writer.step_to`）。
不写 `approval.requested` 是因为 token 的生命周期属于 `HumanCenter`，这里只报告
「哪几步需要人」，由 `Runner` 去问。
"""

from __future__ import annotations

from typing import Any

from ..contracts import (
    EventType,
    Idempotency,
    SideEffect,
    Step,
    StepOutcome,
    ToolSpec,
    VerifyOutcome,
)
from .fold import RunState, StepState
from .writer import Writer

#: 重排队原因（写进 `step.invalidated.reason`，日志里要能一眼看出为什么重跑）。
RETRY_NATURAL = "idempotent_retry"
RETRY_KEYED = "keyed_retry"
RETRY_APPROVED = "approved_in_doubt"
DRIFT = "drift"

#: 与 workspace 内容耦合的内置工具：它们读/写/执行的就是指纹覆盖的那些文件。
#: 新增工具要么进这个集合，要么在 `ToolSpec` 上诚实地声明 `readonly=False`。
WORKSPACE_TOOLS: frozenset[str] = frozenset({"fs.read", "fs.list", "fs.write", "shell.exec"})


# --------------------------------------------------------------------------- #
# in-doubt
# --------------------------------------------------------------------------- #

def is_in_doubt(state: StepState) -> bool:
    """这一步「开始了但没结束」：日志说不清它到底做没做。

    用 seq 比较而不是布尔：同一步可以「开始→结束→又开始」，只有开始之后
    没有结束才算 in-doubt；已经结束过的旧 attempt 不该让新 attempt 变成悬案。
    """
    if state.status != "running":
        return False
    return state.finished_seq <= state.started_seq


def in_doubt_steps(run: RunState) -> list[str]:
    """按计划拓扑序列出 in-doubt 的 step_id（顺序稳定，重放可复现）。

    没有 plan 的 run 不算有「步骤」：那种日志已经被破坏，交给 `drive` 判失败，
    不在这里猜（否则会出现「问人 → 人批准 → 又没有可比对的 payload → 再问」的死循环）。
    """
    if run.plan is None:
        return []
    return [step.id for step in run.plan.order() if is_in_doubt(run.steps[step.id])]


def last_attempt(run: RunState, step_id: str) -> dict[str, Any] | None:
    """该 step 最后一次 `step.started` 留下的身份；从没开始过则 None。

    这是「按幂等键向工具查一次」的输入：`idempotency_key` 相同才允许当同一次调用对待。
    """
    state = run.steps.get(step_id)
    if state is None or not state.idempotency_key:
        return None
    return {
        "step_id": step_id,
        "attempt": state.attempts,
        "idempotency_key": state.idempotency_key,
        "started_seq": state.started_seq,
        "finished_seq": state.finished_seq,
    }


def spec_of(registry: Any, tool: str) -> ToolSpec | None:
    """注册表里查一个工具声明；查不到（或没有注册表）返回 None——「不知道」不是「安全」。"""
    if registry is None:
        return None
    try:
        spec = registry.get(tool)
    except KeyError:
        return None
    return spec if isinstance(spec, ToolSpec) else None


def retry_policy(step: Step | None, registry: Any) -> Idempotency:
    """能不能安全重跑。查不到声明一律按 `NONE`（不可幂等 → 不猜）。"""
    if step is None:
        return Idempotency.NONE
    spec = spec_of(registry, step.tool)
    if spec is None:
        return Idempotency.NONE
    return spec.idempotency


def _requeue(
    writer: Writer,
    run_id: str,
    step_id: str,
    reason: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    """`running → stale → pending`：先留一条 `step.invalidated`（为什么），再回队列。"""
    payload: dict[str, Any] = {"step_id": step_id, "reason": reason}
    if detail is not None:
        payload["detail"] = detail
    writer.emit(EventType.STEP_INVALIDATED, payload, run_id=run_id, step_id=step_id)
    writer.step_to(run_id, step_id, "pending", reason)


def reconcile_in_doubt(
    run: RunState,
    registry: Any = None,
    *,
    writer: Writer | None = None,
    approved: frozenset[str] = frozenset(),
) -> list[str]:
    """把 in-doubt 步骤分流。返回**需要人工**的 step_id 列表。

    - `writer` 为空时只分类、不写事件（dry-run / 测试用）；
    - `approved` 里的步骤表示人已经决定「继续」，于是 `NONE` 的不再问，直接回队列；
    - 谁被重排队、为什么，都写在 `step.invalidated{step_id, reason}` 里。
    """
    ask: list[str] = []
    steps = {step.id: step for step in run.plan.steps} if run.plan is not None else {}
    for step_id in in_doubt_steps(run):
        step = steps.get(step_id)
        policy = retry_policy(step, registry)
        if policy is Idempotency.NATURAL:
            if writer is not None:
                _requeue(writer, run.run_id, step_id, RETRY_NATURAL)
        elif policy is Idempotency.KEYED:
            if writer is not None:
                _requeue(writer, run.run_id, step_id, RETRY_KEYED)
        elif step_id in approved:
            if writer is not None:
                _requeue(writer, run.run_id, step_id, RETRY_APPROVED)
        else:
            ask.append(step_id)
    return ask


# --------------------------------------------------------------------------- #
# 环境漂移
# --------------------------------------------------------------------------- #

def workspace_dependent(step: Step, spec: ToolSpec | None) -> bool:
    """这一步的产物是否来自 workspace 文件。

    MVP 判定（三条，宁可多失效一点）：
    1. `step.env_probes` 非空 → 计划自己声明了依赖（但**由 `probe_drift` 逐项判**，
       不走整树指纹）；
    2. 内置 workspace 工具（读/列/写/执行）；
    3. 工具声明不是只读，或声明的副作用不是 NONE（它改了外部世界）。
    """
    if step.env_probes:
        return True
    if step.tool in WORKSPACE_TOOLS:
        return True
    if spec is None:
        return False
    return (not spec.readonly) or spec.side_effect is not SideEffect.NONE


def probe_drift(
    run: RunState, current: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    """逐 probe 比对冻结时的记录与现在的摘要，返回 `{step_id: [变了的路径]}`。

    声明了 `env_probes` 的步骤只吃这一条判据：换了别的文件不算它变了。
    没有冻结记录（旧日志/手工事件）时按"变了"处理——不能证明没变，就别复用结论。
    """
    out: dict[str, list[str]] = {}
    for step in run.plan.steps if run.plan is not None else ():
        if not step.env_probes:
            continue
        before = run.probe_fps.get(step.id, {})
        after = current.get(step.id, {})
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        if changed:
            out[step.id] = changed
    return out


def drift_victims(
    run: RunState,
    registry: Any = None,
    *,
    workspace_changed: bool = True,
    changed_probes: dict[str, list[str]] | None = None,
    current_tree_fp: str | None = None,
    step_end_fps: dict[str, str] | None = None,
) -> list[str]:
    """环境变了之后必须重跑的步骤：**已 passed 且产物真的受影响**。

    三类判据：
    - 声明了 `env_probes` 的步骤：只看自己探测的文件（`changed_probes`）；
    - 其余耦合步骤：优先比步骤结束指纹（缺口 3，`current_tree_fp` vs
      `step_end_fps`）——世界在它结束之后没变就不是受害者；旧日志没有
      结束指纹时退回整树判据（`workspace_changed`）。

    还没 passed 的步骤不需要失效事件：它们本来就没有可信产物，下次照样要跑。
    只有「已经通过的结论」才有作废这回事——作废必须留痕，所以只对 passed 发
    `step.invalidated{reason: "drift"}`。
    """
    if run.plan is None:
        return []
    changed_probes = changed_probes or {}
    victims: list[str] = []
    for step in run.plan.order():
        state = run.steps.get(step.id)
        if state is None or state.status != "passed":
            continue
        if step.env_probes:
            if step.id in changed_probes:
                victims.append(step.id)
            continue
        if not workspace_dependent(step, spec_of(registry, step.tool)):
            continue
        # 缺口 3：有步骤结束指纹（step.fingerprinted）→ 精确比「世界在它结束之后
        # 变没变」——写文件的步骤不被自己的写多失效一次；没有指纹（旧日志）→
        # 退回整树判据：不能证明没变，就不复用结论。
        end_fps = step_end_fps or {}
        if step.id in end_fps:
            if current_tree_fp is not None and end_fps[step.id] != current_tree_fp:
                victims.append(step.id)
        elif workspace_changed:
            victims.append(step.id)
    return victims


def invalidate(
    writer: Writer,
    run_id: str,
    step_id: str,
    reason: str = DRIFT,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    """把一步作废并放回队列，`detail` 写清"具体哪一项变了"（可查，不是一句 drift）。"""
    _requeue(writer, run_id, step_id, reason, detail=detail)


# --------------------------------------------------------------------------- #
# 崩溃窗口：跑完但没判定 / 判定完但没改状态
# --------------------------------------------------------------------------- #

def awaiting_verification(state: StepState) -> bool:
    """`running` + 有 `step.finished` + 没有 `step.verified`：只差判定，**不要重跑工具**。"""
    return (
        state.status == "running"
        and state.finished_seq > state.started_seq
        and state.verified_seq < state.finished_seq
    )


def awaiting_transition(state: StepState) -> bool:
    """`running` + 判定已经写进日志 + 状态还没改：只差一次 `running → passed/failed`。"""
    return state.status == "running" and state.verified_seq > state.finished_seq > state.started_seq


def unreconciled_steps(run: RunState) -> list[str]:
    """按计划顺序列出「工具已经跑完、drive 还没走完」的步骤（恢复时先收拾它们）。"""
    if run.plan is None:
        return []
    out: list[str] = []
    for step in run.plan.order():
        state = run.steps.get(step.id)
        if state is None:
            continue
        if awaiting_verification(state) or awaiting_transition(state):
            out.append(step.id)
    return out


def outcome_from_log(state: StepState) -> StepOutcome:
    """把 `step.finished` 落盘的观测还原成 `StepOutcome`：不碰工具，只回放事实。"""
    return StepOutcome(
        ok=state.result_status == "ok",
        observations=dict(state.observations),
        artifact_refs=tuple(state.artifact_refs),
        error_code=state.error_code,
        attempt=state.attempts,
    )


def verdict_from_log(state: StepState) -> VerifyOutcome:
    """把 `step.verified` 落盘的结论还原成 `VerifyOutcome`：不必再求值一次断言。"""
    return VerifyOutcome(
        passed=state.verified_result == "pass",
        checks=tuple(state.checks),
        reason=state.verified_reason,
    )


__all__ = [
    "DRIFT",
    "RETRY_APPROVED",
    "RETRY_KEYED",
    "RETRY_NATURAL",
    "WORKSPACE_TOOLS",
    "awaiting_transition",
    "awaiting_verification",
    "drift_victims",
    "in_doubt_steps",
    "invalidate",
    "is_in_doubt",
    "last_attempt",
    "outcome_from_log",
    "probe_drift",
    "reconcile_in_doubt",
    "retry_policy",
    "spec_of",
    "unreconciled_steps",
    "verdict_from_log",
    "workspace_dependent",
]

