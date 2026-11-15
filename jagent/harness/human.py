"""JAgent v8 · 人工闸口（HumanCenter）。

门（gate）不是阻塞调用，而是三段事件：
`APPROVAL_REQUESTED + RUN_SUSPENDED` → 等外部决策 → `APPROVAL_RECEIVED + APPROVAL_RESOLVED + RUN_RESUMED`。
挂起是事实，恢复也是事实；两次写在同一个数据源里，重启后仍能重建「谁在等谁」。

三条纪律：
- token 一次性：解析成功后 `approval` 即被清空，重复解析走 duplicate 分支（返回 False，但不抛）；
- 过期即拒绝：过期不是异常，是一条带 `reason: "expired"` 的 resolved 事实；
- 决策词表固定：`approve / amend / reject`，别的直接 ValueError（调用方写错了，不该污染事件流）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from ..contracts import EventType
from .writer import Writer

#: 唯一合法的决策词表。
DECISIONS: frozenset[str] = frozenset({"approve", "amend", "reject"})

#: 缺省有效期（秒）。一小时：够人看到，又不至于让旧 token 永久可用。
DEFAULT_TTL_S = 3600.0


class HumanCenter:
    def __init__(
        self,
        writer: Writer,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._writer = writer
        self._ttl_s = float(ttl_s)
        self._clock = clock

    # ------------------------------------------------------------------ open --
    def request(
        self,
        run_id: str,
        step_id: str,
        payload_hash: str,
        *,
        context: str = "",
        ttl_s: float | None = None,
    ) -> str:
        """登记一次审批请求并挂起 run，返回一次性 token。"""
        token = uuid.uuid4().hex
        expires_at = self._clock() + (self._ttl_s if ttl_s is None else float(ttl_s))

        self._writer.emit(
            EventType.APPROVAL_REQUESTED,
            {
                "step_id": step_id,
                "token": token,
                "payload_hash": payload_hash,
                "expires_at": expires_at,
                "context": context,
                "kind": "gate",
            },
            run_id=run_id,
            step_id=step_id,
        )
        self._writer.emit(
            EventType.RUN_SUSPENDED,
            {"at_step": step_id},
            run_id=run_id,
            step_id=step_id,
        )
        return token

    # ---------------------------------------------------------------- close --
    def resolve(
        self,
        run_id: str,
        token: str,
        decision: str,
        *,
        comment: str = "",
    ) -> bool:
        """解析一次审批。返回 True 表示这次调用真正让 run 前进了。"""
        if decision not in DECISIONS:
            raise ValueError(
                f"unknown decision {decision!r}; expected one of {sorted(DECISIONS)}"
            )

        approval = self._approval_of(run_id)
        if approval is None or approval.get("token") != token:
            # 未知 token / 已经解析过 / 被新的请求覆盖：都是 duplicate，不是错误。
            self._writer.emit(
                EventType.APPROVAL_RESOLVED,
                {"token": token, "applied": False, "duplicate": True, "decision": decision},
                run_id=run_id,
            )
            return False

        expires_at = approval.get("expires_at")
        now = self._clock()
        if expires_at is not None and now > float(expires_at):
            self._writer.emit(
                EventType.APPROVAL_RESOLVED,
                {
                    "token": token,
                    "applied": False,
                    "duplicate": False,
                    "decision": decision,
                    "reason": "expired",
                },
                run_id=run_id,
            )
            return False

        self._writer.emit(
            EventType.APPROVAL_RECEIVED,
            {"token": token, "decision": decision, "comment": comment},
            run_id=run_id,
        )
        self._writer.emit(
            EventType.APPROVAL_RESOLVED,
            {"token": token, "applied": True, "duplicate": False, "decision": decision},
            run_id=run_id,
        )
        # 拒绝不是恢复：`reject` 之后 run 该不该结束由调用方决定（Session 会置 skipped +
        # failed）。这里替它写一条 `run.resumed` 等于说"任务又跑起来了"，是假事实。
        if decision != "reject":
            self._writer.emit(
                EventType.RUN_RESUMED,
                {"from_seq": self._writer.world.seq},
                run_id=run_id,
            )
        return True

    # ----------------------------------------------------------------- read --
    def pending(self, run_id: str) -> dict | None:
        """当前挂起的审批（副本），没有则 None。

        过期与否由调用方按 `expires_at` 判断：本方法只回放事实，不做策略。
        """
        approval = self._approval_of(run_id)
        return None if approval is None else dict(approval)

    # --------------------------------------------------------------- helper --
    def _approval_of(self, run_id: str) -> dict[str, Any] | None:
        run = self._writer.world.runs.get(run_id)
        return None if run is None else run.approval
