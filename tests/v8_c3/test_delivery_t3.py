"""Wave 2 · C3-fix · 系统声明由构造保证恰好一次（C3-01 / D-017）。

口径（用户 2026-11-08 裁决）：

* 模型正文**原样**渲染，不扫描、不删句、不去重；正文里的 DEFAULT 即使出现多次也保留；
* ``SYSTEM_SEPARATOR`` 之后固定追加**一次**系统段：``【声明】`` + 系统声明 + 风险清单；
* ``advice.disclaimer`` 为空 → ``DEFAULT_DISCLAIMER``；非空 → 替代默认；
  若字段自身含 DEFAULT 一次或多次，系统段只保留一处，声明之外的自定义文字按序保留；
* 风险清单是既有的 ``advice.risks``，移入末尾系统段，不新增通用风险。
"""

from __future__ import annotations

from jagent.agent.delivery import DEFAULT_DISCLAIMER, SYSTEM_SEPARATOR, fallback, render
from jagent.contracts import Advice, Citation, Violation


def _advice(**overrides: object) -> Advice:
    base: dict[str, object] = {
        "info": ("PE 为 45",),
        "risks": ("市场风险：价格波动。", "信息风险：来源有限。"),
        "framework": ("先写清目标与约束。",),
        "body": "下面只列判断要点，不给具体动作指令。",
    }
    base.update(overrides)
    return Advice(**base)  # type: ignore[arg-type]


def _sources_block(text: str) -> str:
    return text.split("【信息来源】\n", 1)[1].split("\n\n", 1)[0]


def _system_segment(text: str) -> str:
    """分隔符之后的一切就是系统段（构造上它固定在末尾）。"""

    index = text.rindex(SYSTEM_SEPARATOR)
    return text[index + len(SYSTEM_SEPARATOR) :]


# --------------------------------------------------------------------------- #
# 1. 有 refs：来源由 refs 渲染，忽略模型 sources
# --------------------------------------------------------------------------- #

def test_refs_render_sources_and_model_sources_are_ignored() -> None:
    advice = _advice(
        refs=(
            Citation(step_id="s1", field="data.pe", label="行情", seq=3),
            Citation(step_id="s2", field="stdout", label="", seq=None),
        ),
        sources=("https://model.example/ignored",),
    )

    text = render(advice)

    block = _sources_block(text)
    assert "- 行情（s1#3）" in block
    assert "- s2" in block
    assert "#" not in block.split("- s2", 1)[1]
    # 有 refs 时模型 sources 不参与展示。
    assert "https://model.example/ignored" not in text
    assert "本次未引用外部来源" not in text


# --------------------------------------------------------------------------- #
# 2. 无 refs：保持原行为（用 sources；没有 sources 给固定说明）
# --------------------------------------------------------------------------- #

def test_without_refs_model_sources_are_used() -> None:
    text = render(_advice(sources=("https://example.com/report",)))

    assert "- https://example.com/report" in _sources_block(text)


def test_without_refs_or_sources_keeps_the_no_source_line() -> None:
    text = render(_advice())

    assert "本次未引用外部来源" in _sources_block(text)


# --------------------------------------------------------------------------- #
# 3. 系统段：固定在末尾、分隔符之后、恰好一次
# --------------------------------------------------------------------------- #

def test_system_segment_is_appended_once_at_the_end_after_the_separator() -> None:
    text = render(_advice())

    assert text.count(SYSTEM_SEPARATOR) == 1
    tail = _system_segment(text)
    assert text.endswith(tail)
    assert tail.startswith("【声明】\n")


def test_system_segment_has_exactly_one_default_disclaimer() -> None:
    tail = _system_segment(render(_advice()))

    assert tail.count(DEFAULT_DISCLAIMER) == 1


# --------------------------------------------------------------------------- #
# 4. 模型正文不改写、不去重（正文里的 DEFAULT 原样保留）
# --------------------------------------------------------------------------- #

def test_body_containing_default_is_kept_and_system_segment_still_one() -> None:
    body = f"正文第一句。\n\n{DEFAULT_DISCLAIMER}\n\n{DEFAULT_DISCLAIMER}"

    text = render(_advice(body=body))

    assert body in text
    assert text.count(DEFAULT_DISCLAIMER) == 3  # 正文两处 + 系统段一处
    assert _system_segment(text).count(DEFAULT_DISCLAIMER) == 1


def test_already_formatted_body_is_not_rewrapped_or_scanned() -> None:
    body = f"【说明】\n正文。\n\n【免责声明】\n{DEFAULT_DISCLAIMER}"

    text = render(_advice(body=body))

    assert text.startswith(body)
    assert text.count("【说明】") == 1
    assert _system_segment(text).count(DEFAULT_DISCLAIMER) == 1


# --------------------------------------------------------------------------- #
# 5. 声明字段：非空替代默认；重复 DEFAULT 只在系统段收敛为一处
# --------------------------------------------------------------------------- #

def test_custom_disclaimer_replaces_the_default_without_stacking() -> None:
    text = render(_advice(disclaimer="以上内容不构成投资建议。"))

    tail = _system_segment(text)
    assert "以上内容不构成投资建议。" in tail
    # 非空声明替代默认：默认不出现，也不与自定义叠加。
    assert DEFAULT_DISCLAIMER not in text
    assert tail.count("以上内容不构成投资建议。") == 1


def test_disclaimer_field_with_default_twice_collapses_only_in_the_system_segment() -> None:
    advice = _advice(
        disclaimer=f"自定义开头\n{DEFAULT_DISCLAIMER}\n自定义结尾\n{DEFAULT_DISCLAIMER}",
    )

    text = render(advice)

    tail = _system_segment(text)
    assert tail.count(DEFAULT_DISCLAIMER) == 1
    assert "自定义开头" in tail
    assert "自定义结尾" in tail
    # Advice 字段本身不被修改；收敛只发生在渲染出的系统段。
    assert advice.disclaimer.count(DEFAULT_DISCLAIMER) == 2


def test_fallback_disclaimer_is_not_duplicated() -> None:
    advice = fallback((Violation(code="ADVICE_NO_CITATION", rule_id="advice.no_citation"),))

    assert advice.disclaimer == DEFAULT_DISCLAIMER

    tail = _system_segment(render(advice))

    assert tail.count(DEFAULT_DISCLAIMER) == 1
    assert tail.count("【声明】") == 1


# --------------------------------------------------------------------------- #
# 6. 风险清单：既有 advice.risks，移入末尾系统段
# --------------------------------------------------------------------------- #

def test_risk_list_is_in_the_final_tail_after_the_system_block() -> None:
    text = render(_advice())
    tail = _system_segment(text)

    assert "【风险提示】" in tail
    assert "- 市场风险：价格波动。" in tail
    assert "- 信息风险：来源有限。" in tail
    assert tail.index(DEFAULT_DISCLAIMER) < tail.index("【风险提示】")
    # 系统段是最后的输出：风险清单收尾。
    assert text.rstrip().endswith("- 信息风险：来源有限。")


def test_no_risk_block_when_risks_are_empty() -> None:
    text = render(_advice(risks=()))

    assert "【风险提示】" not in text
    assert _system_segment(text).count(DEFAULT_DISCLAIMER) == 1
