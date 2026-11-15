"""JAgent v8 · 接地语法的唯一实现（确定性；宪法 v2 I9）。

**数字语法只许住在这里**——`jagent/gate/rules.py`（T1 建议对账、T3 reply 扫描）、
`jagent/app/report_delivery.py`（run 收尾复述）都从本模块取同一套"什么算数字、
怎么写才算同一个数"。闸门之间只允许**策略**不同（R3 没有证据包，所以任何数字声明
都算无来源），不允许**语法**各写一套。

2026-11-15 事故就是策略被当成了语法：reply 的"无来源数字"规则被拿去判一段复述观测的
文本，真实价格 17.19 被判成捏造。所以这里提供两类原语，策略在调用方：

- 有证据包（R2 / T1）：:func:`check_numbers` / :func:`ungrounded_numbers` / :func:`body_numbers`
  ——按单位与百分号做等价写法对齐后再比；
- 没有证据包（R3 / reply）：:func:`number_claims` ——只报告"这里有个数字声明"。

本模块只做宪法允许的两件事（I4）：**对账**（数字 / 引用）与**言语行为**（陈述 vs 指令）。
不按领域写词表，不调模型，不写事件。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

#: 检查 id：写进 ``gate.checked.checks[]`` / ``advice.delivered.delivery.checks[]``。
CHECK_NUMBERS = "numbers.grounded"
CHECK_CITATIONS = "citations.grounded"
CHECK_DIRECTIVES = "directives.absent"

# --------------------------------------------------------------------------- #
# 语法（数字 / 日期 / 序号 / 单位；解析规则写死，不进 config）
# --------------------------------------------------------------------------- #

#: 数字本体：``17.19`` / ``1,234`` / ``0.88``。
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: "数字声明"（R3 口径）：带计量单位的数字才值得当作断言。
NUMBER_CLAIM = re.compile(
    r"\d+(?:\.\d+)?\s*%"
    r"|\d{4}\s*年"
    r"|\d+(?:\.\d+)?\s*(?:元|万|亿|美元|港元|块钱)"
    r"|\d+(?:\.\d+)?\s*(?:倍|成|个百分点)"
    r"|\b\d+\.\d+\b"
)

#: 日期（ISO / 中文年月日）与时间（HH:MM、N点M分）。
DATE = re.compile(
    r"\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}"
    r"|\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*[日号])?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
)
CLOCK = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?")
TIME_POINT = re.compile(r"\d{1,2}\s*点(?:\s*\d{1,2}\s*分|半|整)?")

#: 序号：第 N 条 / 行首 1. / （1）。
ORDINAL = re.compile(r"第\s*\d+\s*(?:[条点项步个名位章节])?")
LIST_MARKER = re.compile(r"(?m)^\s*\d+\s*[.)、]\s*")
PAREN_MARKER = re.compile(r"[（(]\s*\d+\s*[)）]")

#: "以下 3 点" 这类自身结构计数：不是时间，也不豁免。
STRUCTURE_PREFIX = ("以下", "下面", "如下", "以上", "上述")
STRUCTURE_SUFFIX = ("建议", "风险", "结论", "事项", "内容", "要点", "问题", "理由", "做法")

#: 单位换算：万/亿/千/百 与 B/M/K。只做"同一数量的不同写法"对齐。
UNIT_SCALE: dict[str, Decimal] = {
    "万": Decimal(10) ** 4,
    "亿": Decimal(10) ** 8,
    "千": Decimal(10) ** 3,
    "百": Decimal(10) ** 2,
    "B": Decimal(10) ** 9,
    "b": Decimal(10) ** 9,
    "M": Decimal(10) ** 6,
    "m": Decimal(10) ** 6,
    "K": Decimal(10) ** 3,
    "k": Decimal(10) ** 3,
}

#: 系统模板段（免责声明 / 信息来源）里的数字不进入正文扫描（T3 负责）。
SYSTEM_BLOCK = re.compile(r"【(?:免责声明|信息来源)】.*?(?=\n【|\Z)", re.DOTALL)


# --------------------------------------------------------------------------- #
# 数字等价写法
# --------------------------------------------------------------------------- #

def canon(value: Decimal) -> str:
    """把数值写成唯一字符串：去掉千分位与无意义的尾零，避免科学计数法。"""

    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def number_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def unit_after(text: str, end: int) -> Decimal | None:
    index = end
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None
    return UNIT_SCALE.get(text[index])


def percent_after(text: str, end: int) -> bool:
    index = end
    while index < len(text) and text[index].isspace():
        index += 1
    return index < len(text) and text[index] in "%％"


def variants(text: str, match: re.Match[str]) -> set[str]:
    """一个数字在文本里的等价写法（原值 / 单位换算 / 百分数换小数）。"""

    value = number_decimal(match.group(0))
    if value is None:
        return set()
    out = {canon(value)}
    scale = unit_after(text, match.end())
    if scale is not None:
        out.add(canon(value * scale))
    if percent_after(text, match.end()):
        out.add(canon(value / 100))
    return out


def looks_like_structure_count(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 8):match.start()]
    if any(word in before for word in STRUCTURE_PREFIX):
        return True
    after = text[match.end():match.end() + 8].lstrip()
    return any(after.startswith(word) for word in STRUCTURE_SUFFIX)


def exempt_spans(text: str) -> list[tuple[int, int]]:
    """不参与"事实性数字"对的区间：日期 / 时刻 / 序号 / 列表标记。"""

    spans: list[tuple[int, int]] = []
    for pattern in (DATE, CLOCK, ORDINAL, LIST_MARKER, PAREN_MARKER):
        spans.extend(match.span() for match in pattern.finditer(text))
    for match in TIME_POINT.finditer(text):
        if not looks_like_structure_count(text, match):
            spans.append(match.span())
    return spans


def in_spans(span: tuple[int, int], spans: Sequence[tuple[int, int]]) -> bool:
    start, end = span
    return any(start >= left and end <= right for left, right in spans)


def body_numbers(text: str) -> list[tuple[str, set[str]]]:
    """正文里的「事实性数字」：豁免日期/时间/序号后按出现顺序返回。"""

    clean = SYSTEM_BLOCK.sub(" ", text)
    spans = exempt_spans(clean)
    claims: list[tuple[str, set[str]]] = []
    for match in NUMBER.finditer(clean):
        if in_spans(match.span(), spans):
            continue
        variants_here = variants(clean, match)
        if variants_here:
            claims.append((match.group(0), variants_here))
    return claims


def all_number_variants(text: str) -> set[str]:
    """证据（观测 / artifact 全文）里所有数字的等价写法；不豁免。"""

    out: set[str] = set()
    for match in NUMBER.finditer(text):
        out |= variants(text, match)
    return out


def number_claims(text: str) -> tuple[str, ...]:
    """R3 口径（没有证据包）的数字声明：只报告"这里有断言"，不做真假判定。"""

    return tuple(match.group(0) for match in NUMBER_CLAIM.finditer(text))


# --------------------------------------------------------------------------- #
# 对账（R2 / T1 共用）
# --------------------------------------------------------------------------- #

def _haystack(values: Iterable[Any]) -> str:
    """事实/证据 → 可比较的文本（JSON 紧凑化，逗号去掉以便千分位归一）。"""

    parts: list[str] = []
    for value in values:
        try:
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):  # pragma: no cover - default=str 已兜底
            parts.append(str(value))
    return ",".join(parts).replace(",", "")


def ungrounded_numbers(text: str, *facts: Any, allowed: Iterable[str] | None = None) -> list[str]:
    """文本里出现、但给定事实里找不到的数字。

    事实就是模型这次能看到的全部输入，所以"回答里的数字 ⊆ 事实里的数字"是复述的
    下限：多出来的只可能来自模型记忆或编造。比较的是**等价写法**（单位换算与百分号：
    事实 ``0.032`` ↔ 正文 ``3.2%``；事实 ``17.19`` ↔ 正文 ``17.19 元``），日期 /
    时刻 / 序号按语法豁免。``allowed`` 供调用方补充"已核对过的等价写法"（T1 引用对账）。
    """

    fact_text = _haystack(facts)
    known = all_number_variants(fact_text) | set(allowed or ())
    missing: list[str] = []
    for raw, variants_here in body_numbers(text):
        if variants_here & known:
            continue
        # 退路：正文里的原样数字直接出现在事实文本里（如"共 2 步"里的计数）。
        if raw.replace(",", "") in fact_text:
            continue
        missing.append(raw)
    return missing


def check_numbers(text: str, *facts: Any) -> tuple[bool, tuple[str, ...]]:
    """``numbers.grounded``：返回 ``(是否通过, 未找到的数字)``。"""

    missing = tuple(ungrounded_numbers(text, *facts))
    return (not missing, missing)
