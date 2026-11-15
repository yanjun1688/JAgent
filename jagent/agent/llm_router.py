"""LLM 意图路由（Turn 层）。

模型只回答一件事：这个请求属于哪一类（gather / execute / advise）。
三类判据（grounding §1）：

- ``gather``：需要外部事实 + 结论是价值判断（先取数，再判断）；
- ``execute``：有可机器验证的完成态；
- ``advise``：不需要外部事实。

``gather`` 只表示"先取数再判断"这一意图类型，本层**不设** ``Plan.then``——
计划由 Planner 起草，``then`` 由 D 负责。
其余判定全部留在本地，模型无权决定：

- ``missing_params`` 一律由 ``slots.missing`` 计算，模型说的"缺什么"不采信；
- ``single_step`` 只看文本形态（URL / "返回 code"），且只在 execute 时置真；
- 解析失败、字段非法、confidence 非数值、模型不可用 —— 全部不抛，
  降级成 ``confidence=0`` 的 ADVISE，让上层先问"要建议还是要执行"。
- confidence 越界 clamp 到 [0, 1]；type 不在枚举里按降级处理。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Sequence

from ..config import Config, load_config
from ..contracts import IntentDecision, IntentType, Slot
from ..ports import ModelPort, ModelReply, ModelUnavailable

#: type 枚举之外的值一律降级。
_ALLOWED_TYPES: dict[str, IntentType] = {
    IntentType.ADVISE.value: IntentType.ADVISE,
    IntentType.EXECUTE.value: IntentType.EXECUTE,
    IntentType.GATHER.value: IntentType.GATHER,
}

_URL = re.compile(r"https?://\S+")
_RETURN_CODE = re.compile(r"返回\s*code|code\s*\d{3}")

_TEXT_CLIP = 2000
_TURN_CLIP = 200
_RECENT_TURNS = 6


class _BadFieldError(ValueError):
    """模型给了 JSON，但字段非法；args[0] 是降级原因码。"""


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _extract_object(raw_text: str) -> dict[str, Any]:
    """从模型输出里取出一个 JSON 对象；容忍 ```json 围栏与前后噪音。"""

    raw = (raw_text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply is not a JSON object")
    return data


def _slot_lines(slots: dict[str, Slot]) -> list[str]:
    lines = [f"- {name}: {_clip(slot.value, 120)}" for name, slot in slots.items()]
    return lines or ["-（无）"]


def _turn_lines(turns: Sequence[str]) -> list[str]:
    recent = [_clip(turn, _TURN_CLIP) for turn in list(turns)[-_RECENT_TURNS:]]
    return [f"- {turn}" for turn in recent] or ["-（无，这是第一轮）"]


def _memory_lines(memory: Sequence[str]) -> list[str]:
    lines = [f"- {_clip(item, 200)}" for item in memory]
    return lines or ["-（无）"]


def _prompt(text: str, slots: dict[str, Slot], turns: Sequence[str], memory: Sequence[str] = ()) -> str:
    parts = [
        "用户本轮输入：",
        _clip(text, _TEXT_CLIP) or "（空）",
        "",
        "已有槽位（只用于理解上下文，缺失判断由本地完成）：",
        *_slot_lines(slots),
        "",
        f"历史轮数：{len(turns)}",
        "最近几轮：",
        *_turn_lines(turns),
    ]
    if memory:
        # D-030 L2：长期记忆是人确认过的背景，只用于理解，事实仍需当前证据验证。
        parts += ["", "长期记忆（人确认过；其中事实必须由当前证据重新验证）：", *_memory_lines(memory)]
    parts += ["", "只输出 JSON 对象。"]
    return "\n".join(parts)


def _intent_of(data: dict[str, Any]) -> IntentType:
    raw = data.get("type")
    key = raw.strip().lower() if isinstance(raw, str) else ""
    intent = _ALLOWED_TYPES.get(key)
    if intent is None:
        raise _BadFieldError(f"bad_type:{raw!r}")
    return intent


def _confidence_of(data: dict[str, Any]) -> float:
    raw = data.get("confidence")
    if raw is None or isinstance(raw, bool):
        raise _BadFieldError("bad_confidence")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            raise _BadFieldError("bad_confidence") from None
    else:
        raise _BadFieldError("bad_confidence")
    if not math.isfinite(value):
        raise _BadFieldError("bad_confidence")
    return min(max(value, 0.0), 1.0)


def _single_step_marker(text: str) -> str:
    """单步捷径：URL 或"返回 code"形态。只做形态判断，不听模型。"""

    if _URL.search(text):
        return "url"
    if _RETURN_CODE.search(text):
        return "return_code"
    return ""


class LLMRouter:
    """``RouterPort`` 的 LLM 实现；任何失败都降级，不向上抛。"""

    def __init__(self, model: ModelPort, *, config: Config | None = None) -> None:
        self._model = model
        #: 系统提示词是数据：config/prompts/router.md（缺文件在 load_config 已拒绝启动）。
        self._config = config if config is not None else load_config()
        self._system = self._config.prompts["router"]

    def route(
        self,
        *,
        text: str,
        slots: dict[str, Slot],
        turns: Sequence[str],
        memory: Sequence[str] = (),
    ) -> IntentDecision:
        raw = (text or "").strip()
        try:
            reply = self._model.complete(
                system=self._system,
                prompt=_prompt(raw, slots, turns, memory),
                temperature=0.0,
            )
        except ModelUnavailable:
            return self._fallback("model_unavailable", slots)
        except Exception:  # noqa: BLE001 —— 模型层的任何异常都按"不可用"降级
            return self._fallback("model_error", slots)

        try:
            data = _extract_object(self._text_of(reply))
        except Exception:  # noqa: BLE001 —— 非 JSON / 非对象
            return self._fallback("parse_error", slots)

        try:
            intent = _intent_of(data)
            confidence = _confidence_of(data)
        except _BadFieldError as exc:
            return self._fallback(str(exc), slots)

        reason = _clip(data.get("reason"), 300) or f"llm:{intent.value}"
        marker = _single_step_marker(raw)
        single_step = bool(marker) and intent is IntentType.EXECUTE
        if single_step:
            reason = f"{reason}; single_step={marker}"

        decision = IntentDecision(
            type=intent,
            confidence=confidence,
            missing_params=(),
            single_step=single_step,
            reason=reason,
        )
        return decision

    @staticmethod
    def _text_of(reply: ModelReply) -> str:
        text = getattr(reply, "text", "")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _fallback(code: str, slots: dict[str, Slot]) -> IntentDecision:
        """意图失败兜底：不猜缺什么槽位，澄清由 planner 的 clarify 模式现场决定。"""
        return IntentDecision(
            type=IntentType.ADVISE,
            confidence=0.0,
            missing_params=(),
            reason=f"router_fallback:{code}",
        )


def __getattr__(name: str) -> str:
    """兼容入口：``SYSTEM_PROMPT`` 已外置到 ``config/prompts/router.md``。

    运行期一律用注入的 ``Config``；这里只为仍在 ``from ... import SYSTEM_PROMPT``
    的既有测试保留，内容同样读配置文件，不是内联字符串，也不是运行期兜底。
    """
    if name == "SYSTEM_PROMPT":
        return load_config().prompts["router"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
