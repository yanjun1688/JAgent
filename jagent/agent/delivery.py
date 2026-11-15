"""投递层：把 Advice 渲染成给用户看的多段文本；以及未过闸时的兜底。

这里只放**结构性**的通用模板（怎么把一段合规文本排好版、未过闸时诚实说为什么）。
领域内容（某种产品/市场的话术框架、行业风险清单）不属于这里——
那类知识要么由模型在 Advice 里现产，要么不该存在。
"""

from __future__ import annotations

from typing import Sequence

from ..contracts import Advice, Citation, Violation

DEFAULT_DISCLAIMER = (
    "以上内容仅为信息整理与通用决策框架，不构成任何具体行动指令。"
    "请基于你自己的事实与风险承受能力做判断。"
)

#: 模型正文与末尾系统声明段之间的固定分隔符（T3 / C3-01）。
SYSTEM_SEPARATOR = "\n\n---\n\n"

_GENERIC_FRAMEWORK: tuple[str, ...] = (
    "先写清目标：要满足的条件、放弃的条件分别是什么。",
    "再写清约束：期限、可承受的代价、这笔资源的用途。",
    "把问题拆成可核对的事实：来源、口径、时间点是否一致。",
    "至少比较两种方案，分别写上代价与放弃条件。",
    "先用小额、可逆的方式验证判断，再决定是否扩大。",
)

_GENERIC_RISKS: tuple[str, ...] = (
    "信息风险：回答只基于你提供的或可为核的信息，可能存在滞后、遗漏或口径不一致。",
    "时效风险：状态变化后结论会失效；本次回答不代表结论长期成立。",
)


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(item if item.startswith("- ") else f"- {item}" for item in items)


def _ref_line(citation: Citation) -> str:
    """一条引用 → ``label（step_id#seq）``；``seq is None`` 时不渲染 ``#seq``。"""

    where = citation.step_id if citation.seq is None else f"{citation.step_id}#{citation.seq}"
    label = citation.label.strip()
    return f"{label}（{where}）" if label else where


def _source_lines(advice: Advice) -> list[str]:
    """有 refs 用 refs 渲染；没有 refs 才用模型给的 sources（grounding §2.2）。"""

    if advice.refs:
        return [_ref_line(citation) for citation in advice.refs]
    return list(advice.sources)


def _system_block(advice: Advice) -> str:
    """系统声明文本：字段非空则替代默认，从不叠加两者。

    字段里若已经包含 ``DEFAULT_DISCLAIMER``（一次或多次），系统段仍只保留**一处**：
    第一个完整声明保留，其余重复声明删掉，声明之外的自定义文本按原顺序保留
    （C3-01 的"disclaimer contains DEFAULT twice -> 系统段仍只有一处"）。
    """

    custom = advice.disclaimer.strip()
    if not custom:
        return DEFAULT_DISCLAIMER
    first = custom.find(DEFAULT_DISCLAIMER)
    if first < 0:
        return custom
    tail = custom[first + len(DEFAULT_DISCLAIMER) :].replace(DEFAULT_DISCLAIMER, "")
    return f"{custom[:first]}{DEFAULT_DISCLAIMER}{tail}".strip()


def _system_segment(advice: Advice) -> str:
    """末尾固定系统段：``【声明】`` + 系统声明 + 风险清单（T3 / C3-01）。"""

    blocks = [f"【声明】\n{_system_block(advice)}"]
    if advice.risks:
        blocks.append(f"【风险提示】\n{_bullets(advice.risks)}")
    return "\n\n".join(blocks)


def render(advice: Advice) -> str:
    """多段文本：模型正文原样渲染，末尾追加一次固定系统声明段。

    C3-01 的口径是**系统声明恰好一次（构造保证），模型正文不改写、不去重**：

    * 模型正文（说明 / 关键信息 / 决策框架 / 信息来源）原样排版，不扫描、不删句；
    * ``SYSTEM_SEPARATOR`` 之后固定追加 ``【声明】`` + ``system_block`` + 风险清单；
    * ``advice.disclaimer`` 非空时替代默认声明（不叠加）；为空时用 ``DEFAULT_DISCLAIMER``。

    有 ``refs`` 时【信息来源】由 ``refs`` 渲染，忽略模型的 ``sources``（grounding §2.2）。
    """

    body = advice.body.strip()
    blocks: list[str] = []
    if body.startswith("【"):
        # 已排版正文：原样保留，不再包一层【说明】。
        blocks.append(body)
    elif body:
        blocks.append(f"【说明】\n{body}")
    if advice.info:
        blocks.append(f"【关键信息】\n{_bullets(advice.info)}")
    if advice.framework:
        blocks.append(f"【决策框架】\n{_bullets(advice.framework)}")
    sources = _source_lines(advice)
    if sources:
        blocks.append(f"【信息来源】\n{_bullets(sources)}")
    else:
        blocks.append("【信息来源】\n本次未引用外部来源，正文不含未标注来源的数据。")

    model_text = "\n\n".join(blocks)
    if not model_text:
        return _system_segment(advice)
    return f"{model_text}{SYSTEM_SEPARATOR}{_system_segment(advice)}"


def fallback(violations: Sequence[Violation]) -> Advice:
    """未过闸时的兜底：为什么不能给结论 / 通用框架 / 风险清单 / 还要什么信息。"""

    codes = tuple(dict.fromkeys(v.code for v in violations))
    if codes:
        why = (
            "本次模型输出未通过合规校验，违规码【"
            + "、".join(codes)
            + "】。按规则，存在这些问题的内容不会作为结论发给你。"
        )
    else:
        why = "本次输出没有给出可核对的依据。"

    return Advice(
        info=("想要结论的话，先给我可核对的事实：对象、来源、期限各是什么。",),
        risks=_GENERIC_RISKS,
        framework=_GENERIC_FRAMEWORK,
        sources=(),
        disclaimer=DEFAULT_DISCLAIMER,
        body=why + "\n下面只给通用的判断框架，不给任何具体动作指令。",
    )
