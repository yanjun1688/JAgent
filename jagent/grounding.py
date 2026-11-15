"""JAgent v8 · 接地检查（单一实现，确定性）。

宪法 v2 I9：**Guide 必须配 Sensor**。这条模块就是 sensor 的唯一实现处——
``gate/rules.py``（建议对账）、``app/session.py``（reply 扫描）、``app/report_delivery.py``
（run 收尾复述）都调这里，不再各写一套"什么算有来源"。

2026-11-15 事故：三处各有一套判断，reply 扫描把 run 观测里的真实价格判成捏造，
用户只看到"1/1 步通过"。**一个检查只有一份实现**是这条事故的直接结论。

本模块只做两件宪法允许的事（I4）：**对账**（数字 / 引用）与**言语行为**（陈述 vs 指令）。
不按领域写词表，不调模型，不写事件。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

#: 检查 id：写进 ``gate.checked.checks[]`` / ``advice.delivered.delivery.checks[]``。
CHECK_NUMBERS = "numbers.grounded"
CHECK_CITATIONS = "citations.grounded"
CHECK_DIRECTIVES = "directives.absent"

#: 数字 token：``17.19`` / ``1,234`` / ``0.88``（与 gate 的数字抽取同一语法）。
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _haystack(values: Iterable[Any]) -> str:
    """事实/证据 → 可比较的文本（JSON 紧凑化，逗号去掉以便千分位归一）。"""

    parts: list[str] = []
    for value in values:
        try:
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):  # pragma: no cover - default=str 已兜底
            parts.append(str(value))
    return ",".join(parts).replace(",", "")


def ungrounded_numbers(text: str, *facts: Any) -> list[str]:
    """文本里出现、但给定事实里找不到的数字。

    事实就是模型这次能看到的全部输入，所以"回答里的数字 ⊆ 事实里的数字"是复述的
    下限：多出来的只可能来自模型记忆或编造。允许取整/截断（事实 ``0.880282`` →
    文本 ``0.88``）：某个事实数字以它开头即可。
    """

    haystack = _haystack(facts)
    known = set(_NUMBER.findall(haystack))
    missing: list[str] = []
    for token in _NUMBER.findall(text):
        raw = token.replace(",", "")
        if raw in haystack:
            continue
        if len(raw) >= 2 and any(number.startswith(raw) for number in known):
            continue
        missing.append(token)
    return missing


def check_numbers(text: str, *facts: Any) -> tuple[bool, tuple[str, ...]]:
    """``numbers.grounded``：返回 ``(是否通过, 未找到的数字)``。"""

    missing = tuple(ungrounded_numbers(text, *facts))
    return (not missing, missing)
