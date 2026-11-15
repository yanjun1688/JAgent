"""建议侧合规闸门 advice.v8.2（T1 对账 + T2 评审接线）。

T1（本文件，纯确定性、不调用模型）只有四条**对账**规则，检查"结论与证据
对不对得上"，不检查"这句话该不该说"：

``advice.no_citation`` / ``advice.citation_unresolved`` /
``advice.number_ungrounded`` / ``advice.evidence_incomplete``。

"该不该说"属于语义判断，交 T2 judge（:mod:`jagent.harness.judge`）。
judge 只能否决、不能放行（没有 PASS 语义）；judge 也不能把 T1 的失败改成通过。

``mode`` / ``partial`` 由本组件推导，调用方（Session / 模型）都不设置：

- **没有证据要求**（``plan is None`` / ``plan.evidence_requires`` 为空） ->
  ``framework``（跳过 T1，T2 照常跑）；
- **有证据要求但没齐**——包括 ``evidence is None``（"没有证据包"不等于
  "没有证据要求"）—— -> ``insufficient_evidence``；
- 其余 -> ``grounded``（**无论 T1 是否通过**，诊断上要和"证据不齐"分开）。

``partial`` 与 ``mode`` 正交：存在一个 ``status != "ok"`` 且 ``Step.required``
为 ``False`` 的步骤时 ``partial=True``，``mode`` 不变。"非必需"只由
``Step.required`` 表达，``evidence_requires`` 只服务 ``evidence_incomplete``
（D-013）。

数字的**语法**（抽取/豁免/等价写法）住在 :mod:`jagent.grounding`（I9：一个检查
只有一份实现）；本文件只保留**策略**：哪种情形算违规、报什么 rule_id。闸门之间允许
策略不同（reply 没有证据包，所以任何数字声明都是无来源），不允许语法各写一套。
``evidence_truncated`` 是 ``advice.number_ungrounded`` 的 ``reason``，不是第五个 rule_id。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from ..contracts import (
    Advice,
    Citation,
    EvidenceBundle,
    EvidenceItem,
    Fallback,
    GateVerdict,
    JudgeRecord,
    Plan,
    Violation,
)
from ..evidence import missing
from ..grounding import all_number_variants, body_numbers, number_claims
from ..harness.judge import JudgeRequest, JudgeVerdict

RULESET = "advice.v8.2"

#: 随包发布的策略文档版本。改它必须同时在偏差台账登记（check_invariants 第 6 条）。
POLICY_VERSION = "1"

#: T1 的四个 rule_id。invariant 3 逐字比对这四个字面量，改集合必须同 commit 改脚本。
_RULE_NO_CITATION = "advice.no_citation"
_RULE_CITATION_UNRESOLVED = "advice.citation_unresolved"
_RULE_NUMBER_UNGROUNDED = "advice.number_ungrounded"
_RULE_EVIDENCE_INCOMPLETE = "advice.evidence_incomplete"

_CODE_NO_CITATION = "ADVICE_NO_CITATION"
_CODE_CITATION_UNRESOLVED = "ADVICE_CITATION_UNRESOLVED"
_CODE_NUMBER_UNGROUNDED = "ADVICE_NUMBER_UNGROUNDED"
_CODE_EVIDENCE_INCOMPLETE = "ADVICE_EVIDENCE_INCOMPLETE"

#: judge 的否决码。``policy_reject`` 是 **code**，不是 advice.* rule_id：
#: rule_id 用非 advice 命名空间的 ``judge.policy``，避免把 T2 混进 T1 的规则集。
_CODE_POLICY_REJECT = "policy_reject"
_RULE_JUDGE_POLICY = "judge.policy"

#: ``evidence_incomplete`` 的兜底 reason：reason 是违规语义，不是 rule_id。
_FALLBACK_EVIDENCE_INCOMPLETE = "evidence_incomplete"


def _violation(code: str, rule_id: str, detail: str) -> Violation:
    return Violation(code=code, rule_id=rule_id, detail=detail)


# --------------------------------------------------------------------------- #
# reply 侧扫描（独立 profile，不属于 advice T1）
# --------------------------------------------------------------------------- #

#: reply 的规则 id 用 ``reply.*`` 命名空间。v8.1 的四条语义规则已从建议闸门
#: 删除，这里保留的是 reply（闲聊直答）自己的兜底扫描；它不构成 advice T1
#: 的第 5/6 条规则。
_REPLY_RULES: dict[str, str] = {
    "REPLY_DIRECTIVE": "reply.directive",
    "REPLY_UNSOURCED_CLAIM": "reply.unsourced_claim",
}

#: 指令性表述：出现即视为替用户下动作指令。
_DIRECTIVE = re.compile(
    r"买入|卖出|建议买|建议卖|全仓|清仓|加仓|减仓|抄底|梭哈|满仓|杀入"
    r"|稳赚|必赚|包赚|必涨|稳赢|保本保息|无风险套利|闭眼买"
)

#: reply 侧需要溯源的数字声明：百分比、四位年份、金额、倍数、小数。
def scan_reply(text: str) -> tuple[Violation, ...]:
    """reply 模式（无需工具的直接回答）的合规扫描。

    reply 不走 AdviceGate 的 T1 对账（闲聊没有证据包），但指令性表述与
    无来源数字声明同样不允许绕过闸门：命中即拦截，投递走兜底文本。
    """

    violations: list[Violation] = []
    directive = _DIRECTIVE.search(text)
    if directive is not None:
        violations.append(
            _violation(
                "REPLY_DIRECTIVE",
                _REPLY_RULES["REPLY_DIRECTIVE"],
                f"回答出现指令性表述：{directive.group(0)}",
            )
        )
    claims = number_claims(text)
    if claims:
        violations.append(
            _violation(
                "REPLY_UNSOURCED_CLAIM",
                _REPLY_RULES["REPLY_UNSOURCED_CLAIM"],
                f"回答出现数字声明「{claims[0]}」但没有可核对来源",
            )
        )
    return tuple(violations)


def _judge_model(judge: Any) -> str:
    for name in ("model", "model_name", "name"):
        value = getattr(judge, name, None)
        if isinstance(value, str) and value:
            return value
    return type(judge).__name__


def _read_artifact(reader: Any, artifact_id: str) -> str | None:
    """artifact reader 可以是 callable(id)，也可以是带 get_text/read 的 store。"""

    try:
        if hasattr(reader, "get_text"):
            text = reader.get_text(artifact_id)
        elif callable(reader):
            text = reader(artifact_id)
        elif hasattr(reader, "read"):
            text = reader(artifact_id)
        else:
            return None
    except Exception:  # 回查失败按"没有 artifact"处理，不让 gate 抛裸异常
        return None
    return text if isinstance(text, str) else None


def _resolve(
    evidence: EvidenceBundle, citation: Citation
) -> tuple[EvidenceItem | None, str | None]:
    if not citation.step_id or not citation.field:
        return None, None
    for item in evidence.items:
        if item.step_id != citation.step_id:
            continue
        if citation.field in item.observations:
            value = item.observations[citation.field]
            return item, value if value != "" else None
    return None, None


def _is_partial(plan: Plan, evidence: EvidenceBundle) -> bool:
    """``partial`` = 存在 ``status != "ok"`` 且 ``Step.required is False`` 的步骤。

    "非必需"只由 ``Step.required`` 表达（contracts.py:239）；``evidence_requires``
    只服务 ``evidence_incomplete``，不参与 ``partial`` 推导（D-013）。
    """

    required = {step.id: step.required for step in plan.steps}
    return any(
        item.status != "ok" and required.get(item.step_id) is False
        for item in evidence.items
    )


# --------------------------------------------------------------------------- #
# AdviceGate
# --------------------------------------------------------------------------- #

class AdviceGate:
    """建议侧闸门。judge 与 artifact reader 都由构造函数注入（默认无）。"""

    profile: str = "advice"
    ruleset: str = RULESET

    def __init__(
        self,
        judge: Any = None,
        artifact_reader: Any = None,
        *,
        policy_version: str = POLICY_VERSION,
    ) -> None:
        #: judge 为 None = 没接评审：不调用模型；T1 全过时记 ``none``（不代表通过）。
        self._judge = judge
        #: 回查截断观测的全文；可以是 callable(id) 或 ArtifactStore。
        self._artifact_reader = artifact_reader
        self._policy_version = policy_version

    def check(
        self,
        advice: Advice,
        evidence: EvidenceBundle | None = None,
        plan: Plan | None = None,
        *,
        framework_only: bool = False,
    ) -> GateVerdict:
        """审一条建议。

        ``framework_only`` 是 Session 现有的调用形状（``app/session.py`` 传
        ``framework_only=False``）。v8.2 的证据由 D 在 wiring 层接进来之前，
        这里**接受但忽略**它，否则旧调用点会直接崩；不要把它当作模式来源。
        """

        del framework_only  # 临时兼容 Session 的旧调用形状；mode 由 evidence 推导

        effective_plan = plan if plan is not None else Plan(steps=())
        #: framework 的唯一判据是"没有证据要求"（advise / 空计划 / 槽位不齐），
        #: 不是"没拿到证据包"：后者在有 evidence_requires 时按"没齐"处理（I-G3）。
        requires = tuple(plan.evidence_requires) if plan is not None else ()
        violations: list[Violation] = []
        fallback: Fallback | None = None

        if not requires:
            mode = "framework"
            # partial 与 mode 正交：即使框架模式，只要拿得到证据包，
            # 非必需步失败仍要如实标记；没有证据包就无从判断，按 False。
            partial = _is_partial(effective_plan, evidence) if evidence is not None else False
        else:
            unmet = missing(effective_plan, evidence)
            partial = _is_partial(effective_plan, evidence) if evidence is not None else False
            if evidence is None or unmet:
                # "没有证据包"不等于"没有证据要求"：声明了要求就必须对账。
                mode = "insufficient_evidence"
                detail = "缺少证据：" + "、".join(
                    f"{req.step_id}.{req.field}" for req in (unmet or ())
                )
                violations.append(
                    _violation(_CODE_EVIDENCE_INCOMPLETE, _RULE_EVIDENCE_INCOMPLETE, detail)
                )
                fallback = Fallback(reason=_FALLBACK_EVIDENCE_INCOMPLETE, detail=detail)
                # evidence is None 时没有可解析的观测，citation/number 对账无输入。
                if evidence is not None:
                    violations.extend(self._reconcile(advice, evidence, mode))
            else:
                mode = "grounded"
                violations.extend(self._reconcile(advice, evidence, mode))

        judge_record: JudgeRecord | None = None
        if not violations:
            judge_record = self._judge_advice(advice, evidence, mode, violations)

        if fallback is None and violations:
            first = violations[0]
            fallback = Fallback(reason=first.code, detail=first.detail)

        return GateVerdict(
            profile=self.profile,
            passed=not violations,
            violations=tuple(violations),
            ruleset=self.ruleset,
            mode=mode,
            partial=partial,
            fallback=fallback,
            citations=advice.refs,
            judge=judge_record,
        )

    # ------------------------------------------------------------------ T1 --
    def _reconcile(
        self, advice: Advice, evidence: EvidenceBundle, mode: str
    ) -> list[Violation]:
        violations: list[Violation] = []

        if mode == "grounded" and not advice.refs:
            violations.append(
                _violation(
                    _CODE_NO_CITATION,
                    _RULE_NO_CITATION,
                    "grounded 建议的 refs 为空：有数据的结论必须引用至少一条真实观测",
                )
            )

        resolved: list[tuple[Citation, EvidenceItem, str]] = []
        for citation in advice.refs:
            item, value = _resolve(evidence, citation)
            if item is None or value is None:
                violations.append(
                    _violation(
                        _CODE_CITATION_UNRESOLVED,
                        _RULE_CITATION_UNRESOLVED,
                        f"引用解析不到或为空：{citation.step_id}.{citation.field}",
                    )
                )
                continue
            resolved.append((citation, item, value))

        if resolved:
            violations.extend(self._ungrounded_numbers(advice, resolved))
        return violations

    def _ungrounded_numbers(
        self,
        advice: Advice,
        resolved: Sequence[tuple[Citation, EvidenceItem, str]],
    ) -> list[Violation]:
        allowed: set[str] = set()
        truncated_gap = False
        for citation, item, value in resolved:
            allowed |= all_number_variants(value)
            if citation.field not in item.truncated:
                continue
            # 观测被截断：只有能回查 artifact 全文时才算"可核对"。
            texts = [
                text
                for text in (
                    _read_artifact(self._artifact_reader, artifact_id)
                    for artifact_id in item.artifact_ids
                )
                if text is not None
            ]
            if texts:
                for text in texts:
                    allowed |= all_number_variants(text)
            else:
                truncated_gap = True

        body = "\n".join(part for part in (advice.body, *advice.framework) if part)
        violations: list[Violation] = []
        seen: set[str] = set()
        for raw, variants in body_numbers(body):
            if variants & allowed or raw in seen:
                continue
            seen.add(raw)
            if truncated_gap:
                detail = (
                    f"reason=evidence_truncated: 数字「{raw}」不在被截断的观测里，"
                    "且没有可回查的 artifact 全文"
                )
            else:
                detail = f"数字「{raw}」无法从被引用的观测或 artifact 全文里找到"
            violations.append(
                _violation(_CODE_NUMBER_UNGROUNDED, _RULE_NUMBER_UNGROUNDED, detail)
            )
        return violations

    # ------------------------------------------------------------------ T2 --
    def _judge_advice(
        self,
        advice: Advice,
        evidence: EvidenceBundle | None,
        mode: str,
        violations: list[Violation],
    ) -> JudgeRecord:
        """T2：只有 T1 全过才调用 judge；REJECT 即终局，不回传理由、不重生成。

        标记固定四态（D-021）：``reject`` / ``undecidable`` / ``unavailable`` /
        ``none``。judge 抛异常（含 ``ModelUnavailable`` / 解析失败）记
        ``unavailable``；没注入 judge 记 ``none``。两者都放行但标记，绝不改 T1。
        """

        judge = self._judge
        if judge is None:
            return JudgeRecord(verdict="none", reason="judge_not_configured")

        request = JudgeRequest(
            claim="\n".join(part for part in (advice.body, *advice.framework) if part),
            evidence={
                "citations": [
                    {
                        "step_id": citation.step_id,
                        "field": citation.field,
                        "label": citation.label,
                        "seq": citation.seq,
                    }
                    for citation in advice.refs
                ],
                "observations": {
                    f"{item.step_id}.{field}": value
                    for item in (evidence.items if evidence is not None else ())
                    for field, value in item.observations.items()
                },
            },
            # grounding §3 的示例写的是 reason="advice.policy"，但 invariant 3 把
            # advice.* 命名空间逐字留给 T1 的四个 rule_id；T2 的请求标签改用
            # judge.policy，与否决 violation 的 rule_id 同名。
            reason=_RULE_JUDGE_POLICY,
            context={"policy_version": self._policy_version, "mode": mode},
        )
        model_name = _judge_model(judge)
        try:
            outcome = judge.judge(request)
        except Exception:  # noqa: BLE001 - 配了但调不通：放行但标记，绝不放行/改 T1
            return JudgeRecord(
                verdict="unavailable", reason="judge_unavailable", model=model_name
            )

        if outcome is JudgeVerdict.REJECT or getattr(outcome, "value", None) == "reject":
            reason = str(getattr(outcome, "reason", "") or "policy rejected the advice")
            violations.append(
                _violation(_CODE_POLICY_REJECT, _RULE_JUDGE_POLICY, reason)
            )
            return JudgeRecord(verdict="reject", reason=reason, model=model_name)

        if outcome is None:
            return JudgeRecord(
                verdict="undecidable", reason="no_opinion", model=model_name
            )
        return JudgeRecord(
            verdict="undecidable",
            reason=str(getattr(outcome, "reason", "") or "no_opinion"),
            model=model_name,
        )
