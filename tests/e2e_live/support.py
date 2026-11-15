"""E2E 共用：带追踪的模型包装。

真实模型单次调用可能几十秒，不打印就看起来像「卡死」。这个包装在每次调用
前后各打一行：调的是哪一段（工具选择 / 规划 / 收尾）、prompt 多大、耗时多久。
"""

from __future__ import annotations

import sys
import time
from typing import Any


def log(message: str = "") -> None:
    print(message, file=sys.__stdout__, flush=True)


class TracingModel:
    """透明包装 ModelPort：调用前后打印耗时，不改任何行为。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
        self.calls += 1
        if "工具选择器" in system:
            label = "工具选择"
        elif "已有证据" in prompt:
            label = "收尾总结"
        elif "计划器" in system:
            label = "规划"
        elif "路由器" in system:
            label = "意图路由"
        else:
            label = "模型调用"
        log(f"    [模型 #{self.calls}] {label} 请求（prompt {len(prompt)} 字符）…")
        started = time.perf_counter()
        try:
            reply = self._inner.complete(system=system, prompt=prompt, temperature=temperature)
        except Exception as exc:
            log(f"    [模型 #{self.calls}] {label} 失败（{time.perf_counter() - started:.1f}s）：{exc}")
            raise
        log(
            f"    [模型 #{self.calls}] {label} 返回（{time.perf_counter() - started:.1f}s，"
            f"输出 {len(getattr(reply, 'text', '') or '')} 字符）"
        )
        return reply
