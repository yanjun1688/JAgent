"""JAgent v8 · Judge 端口（扩展点，未接线）。

为什么留这个扩展点，以及为什么接口里没有 PASS
--------------------------------------------
v8 的默认规则是「断言由代码判定」：`harness/verify.py` 把 `step.verify.expr`
在观测上求值，求不出来（`evidence_missing` / 语法错误）就按 fail 处理。这条
规则不能因为接了一个模型就反转。

judge 的唯一位置是「代码已经判不了」时求助：它可以指出反证，让失败理由更
具体；也可以说「我也判不了」。它**不能**把一次 fail 改成 pass——所以
`JudgeVerdict` 里根本没有 PASS，`judge()` 返回 `None` 也只表示没有意见。
即使未来有人在 judge 里接了大模型，模型出错最多让一个本来失败的 run 继续
失败，不可能让一个坏 run 变绿。

这也是 v6 用 judge 的方式（`decision/acceptance/judge.py` 只处理
undecidable），但 v6 的端口仍允许 judge 给出通过；v8 把这条权力从类型上
拿掉了，而不是写在注释里靠自觉。

Not wired yet:
* 构造：`jagent/wiring.py:85` 的 `verifier = Verifier()` 可以加一个可选
  `judge=`（默认 `NoJudge()`），不要让 judge 成为必填依赖。
* 调用点：`jagent/harness/verify.py:152-159` 的 `evidence_missing` 分支——
  先确认 `passed=False`，再问 judge；`REJECT` 只把原因写进 `checks`，返回的
  `VerifyOutcome.passed` 仍然是 False。`eval_error` 分支同理。
* 落账：`jagent/app/runner.py:108` 拿到的 `verdict` 照旧写 `STEP_VERIFIED`；
  judge 的意见属于 reason/checks，不能新增一个「judge_passed」的结果态。

接线后的调用形状::

    if not verdict.passed and verdict.reason.startswith("evidence_missing"):
        opinion = judge.judge(JudgeRequest(claim=expr, evidence=observations,
                                          reason=verdict.reason))
        # opinion 只能补充 reason/checks；verdict.passed 保持 False

不做的事：不解析工具输出、不重跑工具、不写事件、不决定重试。judge 看到的
evidence 是已经落盘的观测或摘要；它没有副作用能力，也就不该有「顺手修一下」
的权力。把判 pass 的代码路径收缩到 `verify.py` 的 `passed = bool(value)`，
是这套设计唯一需要长期守住的不变量。

接口为什么这么窄：没有 `ask(question)`，没有 `decide(step)`，也没有拿
Verifier/Registry 的入口。参数固定为 claim + evidence + reason，是为了让
judge 只能「看已经发生的事实」，不能顺手调工具或改计划。将来真需要更多
上下文，应该加进 `JudgeRequest.context`（纯数据），而不是把对象传进去。

一个已知限制：judge 的 REJECT 与代码的 assertion_failed 在事件里同属 fail，
只靠 reason 区分。这是有意的——如果为 judge 单开一个结果态，下一次就会有人
按那个状态做分支，模型就又有了决定权。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

__all__ = ["JudgePort", "JudgeRequest", "JudgeVerdict", "NoJudge"]



class JudgeVerdict(StrEnum):
    """What a judge is allowed to say.

    There is deliberately no PASS member: a model opinion must never be the
    thing that turns a verification failure into a success.
    """

    #: The judge found counter-evidence. It can make a fail more specific.
    REJECT = "reject"
    #: The judge cannot decide either. Same outcome as no judge at all.
    UNDECIDABLE = "undecidable"


@dataclass(frozen=True)
class JudgeRequest:
    """Everything a judge may look at. No tools, no writer, no registry."""

    claim: str
    evidence: Mapping[str, Any]
    #: Why code could not decide, e.g. the `evidence_missing: ...` reason.
    reason: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)


class JudgePort(Protocol):
    def judge(self, request: JudgeRequest) -> JudgeVerdict | None:
        """Return REJECT / UNDECIDABLE, or None for "no opinion"."""
        ...


class NoJudge:
    """Default: there is no judge, and "no judge" means "cannot decide".

    Returning None is not a pass. The caller keeps its fail-closed outcome;
    this class exists so the port can be injected without a model, network or
    key being present.
    """

    def judge(self, request: JudgeRequest) -> None:
        return None
