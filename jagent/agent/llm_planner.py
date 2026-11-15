"""JAgent v8 · LLM 版规划器。

模型面对的是**任意自然语言请求**，它先判断这件事该怎么处理，输出四种 mode 之一：

* ``{"mode":"steps","steps":[...]}``      —— 能用本次工具完成且每步可机器验证；
* ``{"mode":"clarify","questions":[...]}`` —— 缺必需信息，问题由模型按 args schema 现场生成；
* ``{"mode":"advice","advice":{...}}``     —— 结论性建议，交 AdviceGate 合规校验；
* ``{"mode":"reply","text":"..."}``        —— 闲聊/常识问答，交 reply 侧合规扫描。

模型只负责**起草**，不负责**判定**。合法性全部由代码校验，模型说什么都不采信：

steps 模式：
* 工具必须在这次可用的 `tools` 里；
* step id 唯一、非空；
* `depends_on` 只能指向已声明的 step，且无环（`Plan.order()` 兜底）；
* `verify` 必须存在，`expr` 必须能被 `ast.parse(expr, mode="eval")` 解析；
* `verify.needs` 非空，并且是**该工具声明过的观测名**的子集——
  这就是 §7.2 的 `needs ⊆ provides`。这里的严格程度是「名字精确匹配」：
  `observations` 里没有的名字一律拒绝，不给 `ok` 之类的隐式白名单；
  允许集合取「`tools` 里传进来的 descriptor」与「registry 里该工具的 `ToolSpec`」
  的并集（descriptor 被裁剪时仍以 registry 声明为准）；
* 步数 ≤ `max_steps`；单步意图只允许 1 步。

clarify 模式：
* 1–3 个问题；`slot` 名必须匹配 `[A-Za-z_][A-Za-z0-9_]{0,63}`（防事件日志/槽位注入）；
* `text` 非空；slot 不重复。

reply 模式：
* 非空、不超过 2000 字（与输入上限对称；要长产出就该用工具生成产物）。

advice 模式：
* 只做结构校验（body 非空、各字段是字符串数组）；合规判定归 AdviceGate。

任何一条不过就抛 `PlanDraftError`（带**违规清单** `issues`）——不返回半成品计划，
**也不在本层重试**：重试循环只在 Session，每次拒稿落 `plan.rejected`。
`ModelUnavailable` 也包成 `PlanDraftError(outage=True)`，让「模型不可用」与
「草稿不合格」在类型上可区分，而不是只靠文案。

steps 模式里 `on_fail` 缺省 `repair`，`gated` 缺省 False，但
`side_effect == "irreversible"` 的工具一律被本地强制成 `gated=True`（这条不听模型的）。
`args` 原样保留，只要求它是 JSON 可编码的 dict——用 `contracts.canonical_json`
校验，顺带保证 `Plan.hash()` 一定能算。
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..config import Config, load_config
from ..contracts import (
    Advice,
    AdviceDraft,
    ClarifyDraft,
    EvidenceReq,
    IntentDecision,
    MemoryDraft,
    MemoryEntry,
    OnFail,
    Plan,
    PlannerDraft,
    Question,
    ReplyDraft,
    SideEffect,
    Slot,
    Step,
    ToolSpec,
    Verify,
    canonical_json,
    plan_to_dict,
)
from ..harness.plancheck import PlanIssue, check_plan, first_message
from ..ports import ModelPort, ModelUnavailable

#: 代码围栏的标记（解析用，不是 prompt）。
_FENCE = "```"

#: verify 表达式里可用的函数（与 `harness/verify.py` 的白名单保持一致）。
_FUNCS = ("len", "str", "int", "float", "abs", "any", "all")

#: 合法的 on_fail 取值。
_ON_FAIL = frozenset(item.value for item in OnFail)

#: 输出模式 → 各自的 payload 字段名（memory 为 D-030 L2 的长期记忆起草）。
_MODES: dict[str, str] = {
    "steps": "steps",
    "clarify": "questions",
    "reply": "text",
    "advice": "advice",
    "memory": "entries",
}

#: 长期记忆的合法类别（D-030：M5 四类全做）。
_MEMORY_CATEGORIES = frozenset({"user", "feedback", "project", "reference"})

#: clarify 模式的本地校验参数。
_SLOT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_QUESTIONS = 3
_QUESTION_CLIP = 300
_OPTION_CLIP = 60
_MAX_OPTIONS = 6

#: reply / advice 的长度上限（与 turn 输入上限 2000 对称；长产出应走工具+产物）。
_REPLY_CLIP = 2000
_ADVICE_CLIP = 4000


class PlanDraftError(RuntimeError):
    """模型给的草稿过不了本地校验。

    `raw` 是本次原文，`issues` 是违规清单（Session 据此落 `plan.rejected`），
    `plan` 是已成型但未通过自检的计划。`outage=True` 表示失败来自模型层不可用，
    不是草稿质量问题。
    """

    def __init__(
        self,
        reason: str,
        raw: str = "",
        *,
        issues: list[PlanIssue] | None = None,
        plan: Plan | None = None,
        outage: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw
        self.issues = list(issues or ())
        self.plan = plan
        self.outage = outage


def _dump(value: Any, indent: int = 2) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - default=repr 已经兜住绝大多数
        return repr(value)


def _strip_fence(text: str) -> str:
    """容忍模型把 JSON 包在 ```json 围栏里；其余内容原样交给 json.loads。"""
    body = text.strip()
    if not body.startswith(_FENCE):
        return body
    lines = body.splitlines()[1:]
    if lines and lines[-1].strip().startswith(_FENCE):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _index(tools: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """`tools` 列表 → name → descriptor。名字不是非空字符串的条目直接忽略。"""
    indexed: dict[str, dict[str, Any]] = {}
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            indexed[name] = item
    return indexed


def _names(raw: Any) -> list[str]:
    """把 `observations` 读成字符串列表；形状不对就当没声明。"""
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def _evidence_requirements(raw: Any) -> tuple[EvidenceReq, ...]:
    """把模型给的 ``evidence_requires`` 转成 ``EvidenceReq``，**本层不校验**。

    缺字段 / 空值 / 非对象条目一律落成空字符串，交给 plancheck 的草稿自检报
    ``PlanIssue``；`_build` 不会因为一条坏引用就抛新的校验错误、丢掉整份草稿。
    """

    if not isinstance(raw, list):
        return ()
    required: list[EvidenceReq] = []
    for item in raw:
        if not isinstance(item, dict):
            required.append(EvidenceReq(step_id="", field=""))
            continue
        step = item.get("step_id")
        field = item.get("field")
        required.append(
            EvidenceReq(
                step_id=step.strip() if isinstance(step, str) else "",
                field=field.strip() if isinstance(field, str) else "",
            )
        )
    return tuple(required)


def _compact_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """喂给模型的紧凑描述：保留选工具/填参数必需的信息，丢掉长 schema。

    23 个 MCP 工具带完整 JSON Schema 会把 prompt 撑到几十 KB，慢模型直接超时；
    这里只留：名字、参数名与类型、必填项、观测名、副作用、短描述。
    """
    args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
    properties = args.get("properties") if isinstance(args.get("properties"), dict) else {}

    def _property(value: Any) -> Any:
        if not isinstance(value, dict):
            return None
        item: dict[str, Any] = {"type": value.get("type")}
        # 枚举必须保留：丢了它模型会瞎填（例如 adjust="qfq" 而工具只收 forward/backward）。
        enum = value.get("enum")
        if isinstance(enum, list) and enum:
            item["enum"] = enum[:8]
        description = value.get("description")
        if description:
            item["desc"] = str(description)[:60]
        return item

    return {
        "name": tool.get("name"),
        "args": {
            "required": list(args.get("required") or []),
            "properties": {
                key: _property(value) for key, value in properties.items()
            },
        },
        "observations": list(tool.get("observations") or []),
        "side_effect": tool.get("side_effect"),
        "readonly": tool.get("readonly"),
        "description": str(tool.get("description") or "")[:160],
    }


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - IntentDecision 的 confidence 一定是数
        return 0.0


class LLMPlanner:
    """`PlannerPort` 的模型实现。与 `TemplatePlanner` 二选一装配。

    `draft_capable` 为真：除 Plan 外还能产出 Clarify/Reply/Advice 三种草稿，
    Session 据此决定要不要跳过静态槽位表、把澄清权交给模型。
    """

    draft_capable = True

    def __init__(
        self,
        model: ModelPort,
        registry: Any,
        *,
        max_steps: int = 8,
        select_threshold: int = 8,
        max_selected: int = 6,
        config: Config | None = None,
    ) -> None:
        self._model = model
        self._registry = registry
        #: 两段提示词都是数据：config/prompts/planner.md 与 planner_select.md。
        self._config = config if config is not None else load_config()
        self._system = self._config.prompts["planner"]
        self._select_system = self._config.prompts["planner_select"]
        self._max_steps = max(1, int(max_steps))
        #: 工具数超过这个阈值才先做「工具选择」；少量工具直接全给。
        self._select_threshold = max(1, int(select_threshold))
        #: 工具选择最多挑几个。
        self._max_selected = max(1, int(max_selected))

    # ------------------------------------------------------------------ #
    # 工具选择（第一段）
    # ------------------------------------------------------------------ #
    def _select_tools(self, *, text: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """先让模型只看「名字 + 一句话描述」挑工具；失败就退回全量。

        这是**独立阶段、不是重试**：工具数超过阈值时它另计 1 次模型调用，
        所以一次起草尝试的模型调用数是 1（跳过选择）或 2（选择 + 起草）。
        返回的是**原 descriptor 列表的子集**（保持原顺序），后续 planner 只看到这些。
        """
        if len(tools) <= self._select_threshold or not (text or "").strip():
            return tools

        catalog = "\n".join(
            f"- {tool.get('name')}: {str(tool.get('description') or '')[:120]}"
            for tool in tools
        )
        prompt = f"用户请求：\n{(text or '').strip()[:1500]}\n\n工具目录：\n{catalog}\n\n只输出 JSON。"
        try:
            reply = self._model.complete(
                system=self._select_system.replace("{limit}", str(self._max_selected)),
                prompt=prompt,
                temperature=0.0,
            )
        except ModelUnavailable:
            # 模型层不可用不是「选择失败」：不退回全量再打一次，交给 plan() 统一包成 outage。
            raise
        except Exception:  # noqa: BLE001 - 选择失败不阻断，退回全量
            return tools

        picked = self._parse_selection(str(getattr(reply, "text", "") or ""))
        if not picked:
            return tools
        chosen = [tool for tool in tools if tool.get("name") in picked]
        # 模型挑的名字一个都对不上：退回全量，让 planner 自己判断。
        return chosen or tools

    def _parse_selection(self, raw: str) -> list[str]:
        body = _strip_fence(raw)
        if not body:
            return []
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            start, end = body.find("{"), body.rfind("}")
            if start < 0 or end <= start:
                return []
            try:
                data = json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                return []
        if not isinstance(data, dict):
            return []
        items = data.get("tools")
        if not isinstance(items, list):
            return []
        picked = [item for item in items if isinstance(item, str) and item.strip()]
        return picked[: self._max_selected]

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    def _validate_plan(self, plan: Plan, descriptors: list[dict[str, Any]]) -> list[PlanIssue]:
        """草稿自检：不是门，只决定这一稿能不能返回给 Session。

        specs 以 registry 为准；测试/裁剪场景里 descriptor 可能还没进 registry，
        用 descriptor 的 `observations` 补一份只读视图，避免把"本次可见的工具"误判成未知工具。
        """
        specs: dict[str, ToolSpec] = {
            name: self._registry.get(name) for name in self._registry.names()
        }
        for descriptor in descriptors:
            name = descriptor.get("name")
            if not name or name in specs:
                continue
            specs[name] = ToolSpec(
                name=str(name),
                version="1",
                args_schema={},
                observations=tuple(descriptor.get("observations") or ("ok",)),
            )
        return check_plan(plan, specs, max_steps=self._max_steps)

    def plan(
        self,
        *,
        decision: IntentDecision,
        slots: dict[str, Slot],
        tools: list[dict[str, Any]],
        text: str = "",
        replan_of: Plan | None = None,
        failure: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        feedback: list[PlanIssue] | None = None,
        attempt: int = 1,
        turns: Sequence[str] = (),
        memory: Sequence[str] = (),
    ) -> PlannerDraft:
        """一次起草尝试 = 1 次起草调用。

        工具数超过阈值时，工具选择另计 1 次模型调用，它是独立阶段、不是重试；
        因此 `plan()` 的模型调用数是 1 或 2，取决于工具规模。
        重试只在 Session：本层失败就抛 `PlanDraftError`，由调用方决定要不要把
        `issues` 作为 `feedback` 再问一次。本层不循环，也不「放开全量工具再试一次」。
        """
        all_tools = list(tools or [])
        # 第一段（独立阶段，不是重试）：工具数超阈值时，这里会多一次模型调用。
        # 只给「名字 + 一句话描述」让模型挑工具；planner 只看到选中的 schema。
        try:
            available = self._select_tools(text=text, tools=all_tools)
        except ModelUnavailable as exc:
            # 选工具也是一次模型调用：同样转成 outage，不让裸异常逃出去。
            raise self._outage(exc) from exc
        descriptors = _index(available)
        problems: list[str] = [f"{issue.code}: {issue.detail}" for issue in (feedback or ())]
        prompt = self._prompt(
            decision=decision,
            slots=slots,
            tools=available,
            text=text,
            replan_of=replan_of,
            failure=failure,
            problems=problems,
            attempt=attempt,
            evidence=evidence,
            turns=turns,
            memory=memory,
        )
        try:
            reply = self._model.complete(system=self._system, prompt=prompt, temperature=0.0)
        except ModelUnavailable as exc:
            # 不重试：没有模型时重试只是徒劳。原因保留在 message 与 __cause__ 里。
            raise self._outage(exc) from exc

        raw = str(getattr(reply, "text", "") or "")
        parsed, problems = self._parse_mode(raw)
        if parsed is None:
            raise self._draft_error(problems, raw)
        # 已有证据时只允许收尾（advice/reply）；再出 steps 就是没听约束，
        # 立刻失败，调用方会退回普通收尾。
        if evidence is not None and parsed[0] == "steps":
            raise self._draft_error(
                ["已有证据时只允许 advice/reply 模式，模型却输出了 steps"], raw
            )

        kind, payload = parsed
        if kind == "steps":
            records, problems = self._inspect_steps(payload["steps"], descriptors, decision)
            if problems:
                raise self._draft_error(problems, raw)
            plan = self._build(
                records,
                descriptors,
                then=payload.get("then"),
                evidence_requires=payload.get("evidence_requires"),
            )
            try:
                plan.order()  # 最后一道兜底：真成环就拒绝，不放行
            except ValueError as exc:
                raise self._draft_error([f"depends_on 成环：{exc}"], raw) from exc
            issues = self._validate_plan(plan, available)
            if issues:
                raise PlanDraftError(
                    f"计划草稿未通过本地校验：{first_message(issues)}",
                    raw,
                    issues=issues,
                    plan=plan,
                )
            return plan

        if kind == "clarify":
            draft, problems = self._clarify_draft(payload)
        elif kind == "reply":
            draft, problems = self._reply_draft(payload)
        elif kind == "memory":
            draft, problems = self._memory_draft(payload)
        else:  # kind == "advice"
            draft, problems = self._advice_draft(payload)
        if problems:
            raise self._draft_error(problems, raw)
        return draft

    @staticmethod
    def _outage(exc: ModelUnavailable) -> PlanDraftError:
        """模型层不可用：与「草稿不合格」分开，调用方不需要猜文案。"""
        return PlanDraftError(f"模型不可用：{exc}", outage=True)

    @staticmethod
    def _draft_error(problems: list[str], raw: str) -> PlanDraftError:
        """把本地校验的字符串清单包成 `PlanDraftError`，供 Session 直接消费。"""
        detail = "；".join(problems) or "模型没有给出可用输出"
        return PlanDraftError(
            f"计划草稿未通过本地校验：{detail}",
            raw,
            issues=[PlanIssue("INVALID_DRAFT", "", problem) for problem in problems],
        )

    # ------------------------------------------------------------------ #
    # 解析与校验
    # ------------------------------------------------------------------ #

    def _parse_mode(self, raw: str) -> tuple[tuple[str, Any] | None, list[str]]:
        """取 mode 与 payload；形状不对返回 (None, problems)。"""
        body = _strip_fence(raw)
        if not body:
            return None, ["输出为空：需要一个 JSON 对象，mode 取 steps/clarify/reply/advice 之一"]
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            return None, [f"输出不是合法 JSON：{exc}"]
        if not isinstance(data, dict):
            return None, [f"输出必须是 JSON 对象，实际是 {type(data).__name__}"]

        mode = data.get("mode")
        if mode is None and isinstance(data.get("steps"), list):
            mode = "steps"  # 兼容旧格式：只有 steps 数组也认
        if not isinstance(mode, str) or mode not in _MODES:
            return None, [
                f"mode 必须是 {'/'.join(sorted(_MODES))} 之一，实际 {mode!r}"
            ]

        payload = data.get(_MODES[mode])
        if mode == "steps":
            steps = data.get("steps")
            if not isinstance(steps, list):
                return None, ['steps 模式缺少 steps 数组：顶层必须是 {"mode":"steps","steps":[...]}']
            if not steps:
                return None, ["steps 不能为空：至少要有 1 步"]
            then = data.get("then")
            if then is not None and then != "advice":
                return None, [f'then 只能是 "advice" 或不写，实际 {then!r}']
            return (
                "steps",
                {
                    "steps": steps,
                    "then": then,
                    # 形状不在这里校验：缺失/空值/未知 step 都由 plancheck 报 PlanIssue。
                    "evidence_requires": data.get("evidence_requires"),
                },
            ), []
        return (mode, payload), []

    def _inspect_steps(
        self,
        steps: list[Any],
        descriptors: dict[str, dict[str, Any]],
        decision: IntentDecision,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        problems: list[str] = []
        if len(steps) > self._max_steps:
            problems.append(f"步数 {len(steps)} 超过上限 {self._max_steps}")
        single = bool(getattr(decision, "single_step", False))
        if single and len(steps) != 1:
            problems.append(f"这是单步意图，只能输出 1 步，实际 {len(steps)} 步")

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(steps):
            record, step_problems = self._step(index, item, descriptors, seen)
            problems.extend(step_problems)
            if record is not None:
                if record["id"]:
                    seen.add(record["id"])
                records.append(record)

        named = [record for record in records if record["id"]]
        problems.extend(self._dependencies(named))
        return records, problems

    # ------------------------------------------------------------------ #
    # clarify / reply / advice 三种草稿的本地校验
    # ------------------------------------------------------------------ #

    def _clarify_draft(self, payload: Any) -> tuple[ClarifyDraft | None, list[str]]:
        problems: list[str] = []
        if not isinstance(payload, list) or not payload:
            return None, ["clarify.questions 必须是非空数组"]
        if len(payload) > _MAX_QUESTIONS:
            problems.append(f"questions 最多 {_MAX_QUESTIONS} 个，实际 {len(payload)}")

        questions: list[Question] = []
        seen: set[str] = set()
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                problems.append(f"questions[{index}] 必须是 JSON 对象，实际 {type(item).__name__}")
                continue
            raw_slot = item.get("slot")
            slot = raw_slot.strip() if isinstance(raw_slot, str) else ""
            if not slot or not _SLOT_NAME.match(slot):
                problems.append(
                    f"questions[{index}].slot 必须匹配 [A-Za-z_][A-Za-z0-9_]{{0,63}}，实际 {raw_slot!r}"
                )
                continue
            if slot in seen:
                problems.append(f"questions[{index}].slot 重复：{slot!r}")
                continue
            seen.add(slot)

            raw_text = item.get("text")
            text = raw_text.strip() if isinstance(raw_text, str) else ""
            if not text:
                problems.append(f"questions[{index}].text 必须是非空字符串")
                continue
            text = text[:_QUESTION_CLIP]

            raw_options = item.get("options", [])
            options: tuple[str, ...] = ()
            if isinstance(raw_options, list):
                options = tuple(
                    str(option).strip()[:_OPTION_CLIP]
                    for option in raw_options
                    if isinstance(option, str) and option.strip()
                )[:_MAX_OPTIONS]

            questions.append(Question(slot=slot, text=text, options=options))

        if problems:
            return None, problems
        return ClarifyDraft(questions=tuple(questions)), []

    def _reply_draft(self, payload: Any) -> tuple[ReplyDraft | None, list[str]]:
        if not isinstance(payload, str) or not payload.strip():
            return None, ["reply.text 必须是非空字符串"]
        text = payload.strip()
        if len(text) > _REPLY_CLIP:
            return None, [
                f"reply.text 超长（{len(text)} > {_REPLY_CLIP}）：直接回答应精炼；"
                "需要长产出请改用 steps 模式让工具生成产物"
            ]
        return ReplyDraft(text=text), []

    def _memory_draft(self, payload: Any) -> tuple[MemoryDraft | None, list[str]]:
        """D-030 L2：解析 memory 模式的待记条目（起草不等于写入，见 Session）。"""
        if not isinstance(payload, list) or not payload:
            return None, ['memory 模式缺少 entries 数组：{"mode":"memory","entries":[...]}']
        entries: list[MemoryEntry] = []
        problems: list[str] = []
        for item in payload[:10]:
            if not isinstance(item, dict):
                problems.append("entries 的每一项必须是对象 {category, content}")
                continue
            category = str(item.get("category") or "").strip()
            content = str(item.get("content") or "").strip()
            if category not in _MEMORY_CATEGORIES:
                problems.append(
                    f"category 必须是 {'/'.join(sorted(_MEMORY_CATEGORIES))} 之一，实际 {category!r}"
                )
                continue
            if not content:
                problems.append("content 不能为空")
                continue
            if len(content) > 300:
                content = content[:300]
            entries.append(MemoryEntry(category=category, content=content))
        if entries:
            return MemoryDraft(entries=tuple(entries)), []
        return None, problems or ["entries 里没有合法条目"]

    def _advice_draft(self, payload: Any) -> tuple[AdviceDraft | None, list[str]]:
        problems: list[str] = []
        if not isinstance(payload, dict):
            return None, [f"advice 必须是 JSON 对象，实际是 {type(payload).__name__}"]

        def _strs(key: str) -> tuple[str, ...] | None:
            raw = payload.get(key, [])
            if not isinstance(raw, list):
                return None
            return tuple(
                str(item).strip() for item in raw if isinstance(item, str) and item.strip()
            )

        raw_body = payload.get("body")
        body = raw_body.strip() if isinstance(raw_body, str) else ""
        if not body:
            problems.append("advice.body 必须是非空字符串")
        elif len(body) > _ADVICE_CLIP:
            problems.append(f"advice.body 超长（{len(body)} > {_ADVICE_CLIP}）")

        info = _strs("info")
        risks = _strs("risks")
        framework = _strs("framework")
        sources = _strs("sources")
        for name, value in (
            ("info", info), ("risks", risks), ("framework", framework), ("sources", sources)
        ):
            if value is None:
                problems.append(f"advice.{name} 必须是字符串数组")

        raw_disclaimer = payload.get("disclaimer")
        disclaimer = raw_disclaimer.strip() if isinstance(raw_disclaimer, str) else ""

        if problems:
            return None, problems
        return (
            AdviceDraft(
                advice=Advice(
                    info=info or (),
                    risks=risks or (),
                    framework=framework or (),
                    sources=sources or (),
                    disclaimer=disclaimer,
                    body=body,
                )
            ),
            [],
        )

    def _step(
        self,
        index: int,
        item: Any,
        descriptors: dict[str, dict[str, Any]],
        seen: set[str],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        where = f"steps[{index}]"
        if not isinstance(item, dict):
            return None, [f"{where} 必须是 JSON 对象，实际是 {type(item).__name__}"]

        problems: list[str] = []
        raw_id = item.get("id")
        step_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not step_id:
            problems.append(f"{where}.id 必须是非空字符串，实际 {raw_id!r}")
        elif step_id in seen:
            problems.append(f"{where}.id 重复：{step_id!r}（id 必须唯一）")
        name = f"step {step_id!r}" if step_id else where

        tool = item.get("tool")
        tool_name = tool if isinstance(tool, str) else ""
        descriptor: dict[str, Any] | None = None
        if not tool_name:
            problems.append(f"{name}.tool 必须是非空字符串，实际 {tool!r}")
        elif tool_name not in descriptors:
            known = ", ".join(sorted(descriptors)) or "（无）"
            problems.append(f"{name}.tool={tool_name!r} 不在可用工具里（可用：{known}）")
        else:
            descriptor = descriptors[tool_name]

        args = item.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            problems.append(f"{name}.args 必须是 JSON 对象（dict），实际是 {type(args).__name__}")
            args = {}
        else:
            try:
                canonical_json(args)
            except (TypeError, ValueError) as exc:
                problems.append(f"{name}.args 不是 JSON 可编码的 dict：{exc}")

        raw_deps = item.get("depends_on", [])
        if raw_deps is None:
            raw_deps = []
        if not isinstance(raw_deps, list) or any(not isinstance(dep, str) for dep in raw_deps):
            problems.append(f"{name}.depends_on 必须是字符串数组，实际 {raw_deps!r}")
            deps: list[str] = []
        else:
            deps = list(raw_deps)

        on_fail = item.get("on_fail", OnFail.REPAIR.value)
        if not isinstance(on_fail, str) or on_fail not in _ON_FAIL:
            problems.append(
                f"{name}.on_fail 必须是 {'/'.join(sorted(_ON_FAIL))} 之一，实际 {on_fail!r}"
            )
            on_fail = OnFail.REPAIR.value

        gated = item.get("gated", False)
        if not isinstance(gated, bool):
            problems.append(f"{name}.gated 必须是布尔值，实际 {gated!r}")
            gated = False

        expr, needs, verify_problems = self._verify(name, item.get("verify"), tool_name, descriptor)
        problems.extend(verify_problems)

        record = {
            "id": step_id,
            "tool": tool_name,
            "args": args,
            "depends_on": deps,
            "expr": expr,
            "needs": needs,
            "on_fail": on_fail,
            "gated": gated,
        }
        return record, problems

    def _verify(
        self,
        name: str,
        raw: Any,
        tool_name: str,
        descriptor: dict[str, Any] | None,
    ) -> tuple[str, list[str], list[str]]:
        if not isinstance(raw, dict):
            return "", [], [
                f"{name} 缺少 verify：每一步都必须有 "
                '{"expr": "...", "needs": ["..."]}'
            ]

        problems: list[str] = []
        expr = raw.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            problems.append(f"{name}.verify.expr 必须是非空字符串，实际 {expr!r}")
            expr = ""
        else:
            try:
                ast.parse(expr, mode="eval")
            except SyntaxError as exc:
                problems.append(f"{name}.verify.expr 语法错误：{expr!r} -> {exc.msg}")

        raw_needs = raw.get("needs")
        needs: list[str] = []
        if (
            not isinstance(raw_needs, list)
            or not raw_needs
            or any(not isinstance(need, str) or not need.strip() for need in raw_needs)
        ):
            problems.append(f"{name}.verify.needs 必须是非空字符串数组，实际 {raw_needs!r}")
        else:
            needs = [str(need) for need in raw_needs]
            if tool_name and descriptor is not None:
                declared = self._observations(tool_name, descriptor)
                undeclared = [need for need in needs if need not in declared]
                if undeclared:
                    known = ", ".join(sorted(declared)) or "（无）"
                    problems.append(
                        f"{name}.verify.needs 引用了工具 {tool_name!r} 未声明的观测 "
                        f"{', '.join(repr(need) for need in undeclared)}；"
                        f"{tool_name!r} 声明的观测只有：{known}"
                    )
        return expr, needs, problems

    def _dependencies(self, records: list[dict[str, Any]]) -> list[str]:
        """存在性 → 成环 → 声明顺序。三步都过才算合法。"""
        problems: list[str] = []
        ids = [record["id"] for record in records]
        if len(ids) != len(set(ids)):
            return problems  # id 重复已经报过，依赖图不可信，不再二次判定

        by_id = {record["id"]: record for record in records}
        broken = False
        for record in records:
            for dep in record["depends_on"]:
                if dep == record["id"]:
                    problems.append(f"step {record['id']!r}.depends_on 不能依赖自己")
                    broken = True
                elif dep not in by_id:
                    problems.append(
                        f"step {record['id']!r}.depends_on 指向不存在的 step {dep!r}"
                    )
                    broken = True
        if broken:
            return problems

        cycle = self._cycle(records)
        if cycle:
            return [*problems, cycle]

        position = {record["id"]: index for index, record in enumerate(records)}
        for record in records:
            for dep in record["depends_on"]:
                if position[dep] >= position[record["id"]]:
                    problems.append(
                        f"step {record['id']!r}.depends_on 引用了 {dep!r}，"
                        "但依赖只能指向前面已声明的 step"
                    )
        return problems

    @staticmethod
    def _cycle(records: list[dict[str, Any]]) -> str:
        probe = Plan(
            steps=tuple(
                Step(
                    id=record["id"],
                    tool=record["tool"] or "?",
                    depends_on=tuple(record["depends_on"]),
                )
                for record in records
            )
        )
        try:
            probe.order()
        except ValueError as exc:
            return f"depends_on 成环：{exc}"
        return ""

    # ------------------------------------------------------------------ #
    # 工具声明
    # ------------------------------------------------------------------ #

    def _spec(self, name: str) -> Any:
        getter = getattr(self._registry, "get", None)
        if getter is None:
            return None
        try:
            return getter(name)
        except Exception:  # pragma: no cover - registry 不认识这个工具
            return None

    def _observations(self, name: str, descriptor: dict[str, Any]) -> set[str]:
        """声明过的观测名：descriptor 与 registry 的并集（registry 是声明正本）。

        两边的 `observations` 都按「字符串序列」读，形状不对就当作没声明——
        宁可拒绝一个 need，也不让一个假 registry 把校验放空。
        """
        declared: set[str] = set()
        declared.update(_names(descriptor.get("observations")))
        spec = self._spec(name)
        if spec is not None:
            raw = spec.get("observations") if isinstance(spec, dict) else getattr(spec, "observations", None)
            declared.update(_names(raw))
        return declared

    def _side_effect(self, name: str, descriptor: dict[str, Any]) -> str:
        raw = descriptor.get("side_effect")
        if raw is None:
            spec = self._spec(name)
            raw = spec.get("side_effect") if isinstance(spec, dict) else getattr(spec, "side_effect", None)
        value = raw.value if isinstance(raw, SideEffect) else raw
        return value.strip().lower() if isinstance(value, str) else ""

    # ------------------------------------------------------------------ #
    # 组装
    # ------------------------------------------------------------------ #

    def _build(
        self,
        records: list[dict[str, Any]],
        descriptors: dict[str, dict[str, Any]],
        *,
        then: str | None = None,
        evidence_requires: Any = None,
    ) -> Plan:
        steps: list[Step] = []
        for record in records:
            side_effect = self._side_effect(record["tool"], descriptors[record["tool"]])
            gated = bool(record["gated"]) or side_effect == "irreversible"
            steps.append(
                Step(
                    id=record["id"],
                    tool=record["tool"],
                    args=record["args"],
                    depends_on=tuple(record["depends_on"]),
                    verify=Verify(expr=record["expr"], needs=tuple(record["needs"])),
                    on_fail=OnFail(record["on_fail"]),
                    gated=gated,
                )
            )
        return Plan(
            steps=tuple(steps),
            then=then,
            evidence_requires=_evidence_requirements(evidence_requires),
        )

    # ------------------------------------------------------------------ #
    # prompt
    # ------------------------------------------------------------------ #

    def _prompt(
        self,
        *,
        decision: IntentDecision,
        slots: dict[str, Slot],
        tools: list[dict[str, Any]],
        text: str,
        replan_of: Plan | None,
        failure: dict[str, Any] | None,
        problems: list[str],
        attempt: int,
        evidence: dict[str, Any] | None = None,
        turns: Sequence[str] = (),
        memory: Sequence[str] = (),
    ) -> str:
        single = bool(getattr(decision, "single_step", False))
        kind = getattr(decision, "type", "")
        kind_value = getattr(kind, "value", kind)
        confidence = _as_float(getattr(decision, "confidence", 0.0))
        target = getattr(decision, "target_tool", None)
        reason = getattr(decision, "reason", "") or ""
        slot_map = {
            name: {
                "value": getattr(slot, "value", slot),
                "source": getattr(slot, "source", "user"),
            }
            for name, slot in (slots or {}).items()
        }

        lines: list[str] = [
            f"# 第 {attempt} 次起草",
            "",
            "## 用户意图",
            f"- 用户原话：{(text or '').strip() or '（没有提供原文）'}",
            f"- 当前日期：{datetime.now().strftime('%Y-%m-%d')}"
            "（需要日期区间时按它推算，格式按工具 args schema，常见 YYYYMMDD）",
            f"- 意图类型：{kind_value}（置信度 {confidence:.2f}）",
            "- 意图类型只是上游参考；最终走哪种 mode 由你根据可用工具和请求性质决定。",
            f"- 单步意图：{'是，只能输出 1 步' if single else '否'}",
            f"- 目标工具：{target or '未指定'}",
            f"- 路由理由：{reason or '（无）'}",
            "",
            "## 近期对话（按时间序；只用于理解上下文，缺失判断由本地完成）",
            *(
                [f"- {turn}" for turn in list(turns)[-6:]]
                or ["-（无，这是第一轮）"]
            ),
            "",
            *(
                [
                    "## 长期记忆（人确认过；只用于理解用户，其中事实必须由当前证据重新验证）",
                    *[f"- {item}" for item in memory],
                    "",
                ]
                if memory
                else []
            ),
            "## 槽位（name → value/source）",
            _dump(slot_map),
            "已提供的槽位就是已知信息：起草前先检查这里。即使键名与你想问的不完全一致，"
            "只要值可用就直接用，不要重复索要同一项信息；确实缺的才用 clarify。",
            "",
            "",
            "## 可用工具（descriptors，只能用这里的 name）",
            _dump([_compact_tool(tool) for tool in tools if isinstance(tool, dict)]),
            "",
            "## 硬约束（steps 模式专用；clarify/reply/advice 模式不受这些约束）",
            "1. 每一步都必须有 verify：verify.expr 是单个表达式字符串，verify.needs 是非空数组。",
            "2. verify.needs 里的每个名字，都必须是**该工具 observations 里声明过的名字**"
            "（needs ⊆ observations）。不要写函数名，也不要自造观测名；"
            "工具没声明的名字会被本地校验直接拒绝。",
            "2b. 观测的类型（写断言必须类型正确，否则断言会假失败）："
            "`ok` 是布尔（写 `ok` 或 `ok == true`，**不要写 `ok == \"ok\"`**）；"
            "`content` / `stdout` / `stderr` 是字符串（写 `len(content) > 0` 这类）；"
            "`entries` 是数组（写 `len(entries) > 0`）；"
            "`exit_code` 是整数或 null（写 `exit_code == 0`）；"
            "MCP 工具的 `data` 是任意载荷（只判断 `ok`，不要对 `data` 做算术）。",
            "3. verify.expr 只支持：该工具声明的观测名、函数 "
            f"{'/'.join(_FUNCS)}、比较（== != > >= < <=）、布尔（and/or/not）、"
            "in / not in、四则运算；不支持属性访问、下标、lambda、import。",
            f"4. 最多 {self._max_steps} 步。",
            "5. 每个 step 的 id 必须是唯一非空字符串。",
            "6. depends_on 只能引用数组里**前面已经出现过**的 step id，并且不允许成环。",
            "7. on_fail 取 retry|repair|ask_user（缺省 repair）；gated 取 true/false（缺省 false）。",
            "8. side_effect=irreversible 的工具必须 gated=true；这条由本地强制，写 false 也会被改成 true。",
            "9. args 必须是 JSON 对象（dict），按工具 args 的 schema 填。",
            '10. 用户最终要建议时，steps 顶层加 "then":"advice"；系统会基于取数结果再给建议。',
            '11. 引用前一步的结果用 `{{step_id.路径}}`，例如 `{{step_1.data.item.0.thscode}}`；'
            "执行器会替换成真实值，取不到就判这一步失败。MCP 工具的载荷在 `data` 下，"
            "不要引用没出现过的字段。",
        ]

        if evidence:
            lines.extend(
                [
                    "",
                    "## 已有证据（本次 run 的产物）",
                    _dump(evidence),
                    "",
                    "已有证据时**只输出 advice 或 reply 模式**，不要再输出 steps、也不要再要求取数：",
                    "- 用户要的是判断/建议 → advice（必须基于证据里的事实，证据里没有的数字不要写）；",
                    "- 用户只是要查询/操作的结果 → reply：用几段话总结执行结果，"
                    "引用证据里的关键数字和状态，不要编造。",
                ]
            )

        if single:
            lines.extend(
                [
                    "",
                    "本次是单步意图：如果走 steps 模式，steps 数组只能有 1 个元素，不要拆成多步。",
                ]
            )

        if replan_of is not None:
            lines.extend(
                [
                    "",
                    "## 上一版计划（本次是重排，必须给出完整的新计划）",
                    _dump(plan_to_dict(replan_of)),
                    "",
                    "## 上一版失败信息",
                    _dump(failure or {}),
                ]
            )

        if problems:
            lines.extend(
                [
                    "",
                    "## 上一轮输出的违规清单（未通过本地校验），必须逐条修正",
                    *[f"{index}. {problem}" for index, problem in enumerate(problems, 1)],
                ]
            )

        lines.extend(
            [
                "",
                "## 输出格式（只输出一个 JSON 对象，不要别的字；mode 四选一）",
                '{"mode":"steps","steps":[{"id":"step_1","tool":"fs.list","args":{"path":"."},'
                '"depends_on":[],"verify":{"expr":"len(entries) > 0","needs":["entries"]},'
                '"on_fail":"repair","gated":false}],'
                '"evidence_requires":[{"step_id":"step_1","field":"entries"}]}',
                '{"mode":"clarify","questions":[{"slot":"path","text":"要把文件写到哪个路径？",'
                '"options":["当前目录","docs/"]}]}',
                '{"mode":"advice","advice":{"body":"...","risks":["...","..."],'
                '"framework":["..."],"sources":["..."],"disclaimer":"...","info":[]}}',
                '{"mode":"reply","text":"..."}',
                "",
                "steps 模式的 evidence_requires（结论必须引用的观测，可空）：每项 "
                '{"step_id":"...","field":"..."}；step_id 必须是 steps 里的 id，'
                "field 必须是该步工具 observations 里声明过的名字。",
            ]
        )
        return "\n".join(lines)
