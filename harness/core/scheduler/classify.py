"""Intent classification — trusted conservative gate + contract extraction.

Bug JAGENT-2026-P1-13: a real model once answered ``no`` to "does this intent
need tools" for a path-escaping write, bypassing the whole Tool Layer /
Guardrail. The trusted gate runs BEFORE the LLM: if the intent contains a
deterministic tool-operation signal (file/path/URL/browser/workspace), the run
is forced into the Tool Layer regardless of the LLM's answer.
"""

from __future__ import annotations

import asyncio
import re
import time

from harness.core.logger import agent_logger
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import DeliveryContractsResolvedPayload, EventType

_sched_ctrl = agent_logger("scheduler.control")
_sched_think = agent_logger("scheduler.think")

# ── classify 受信保守门（Bug JAGENT-2026-P1-13）──────────────────────
# 不能依赖 LLM 的 "no" 来决定是否进入 Tool Layer —— 真实模型曾对
# "写入 ../blackbox-escape.txt" 返回 no，导致文件写入请求绕过整个
# Guardrail/Tool Layer。这里是受信组件：确定性规则命中即强制
# needs_tools=True，LLM 的 "no" 只在意图无任何工具信号时才生效。

_TOOL_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 文件操作：文件/路径/workspace 语义
    re.compile(r"\b(file|write|read|create|delete|append|list|cat|mkdir|rm|touch)\b", re.IGNORECASE),
    re.compile(r"\.(txt|md|json|yaml|yml|toml|ini|log|csv|py|js|ts|html|css)\b", re.IGNORECASE),
    re.compile(r"(workspace|directory|folder|path|directories|父目录|工作区|目录|路径|文件)", re.IGNORECASE),
    # 路径越界信号
    re.compile(r"(\.\./|\.\.\\|parent dir)", re.IGNORECASE),
    re.compile(r"[a-zA-Z]:[\\/]", re.IGNORECASE),
    # 网络操作
    re.compile(r"https?://|www\.|\.com|\.org|\.io|\.net", re.IGNORECASE),
    re.compile(r"\b(web|search|fetch|http|browser|navigate|api|url|download|upload|curl|request)\b", re.IGNORECASE),
    # MCP / 外部服务
    re.compile(r"\b(mcp|playwright|memory|screenshot|query|execute)\b", re.IGNORECASE),
)

# 上述模式内的词语 —— 纯闲聊也可能出现，故需"强信号"独立判定。
# 若只匹配到这些弱词，仍交由 LLM 决定（但最终仍以 needs_tools 保守为准）。
_WEAK_TOOL_SIGNALS: tuple[str, ...] = ("file", "write", "read", "create", "search", "list")


def intent_requires_tools(intent: str) -> bool:
    """受信保守门：意图是否含确定性工具操作信号。

    Returns True 表示必须进入 Tool Layer（Guardrail 才有机会拦截），
    即使 LLM classify 返回 "no" 也不能绕过。
    """
    if not intent:
        return False
    lowered = intent.lower()
    return any(p.search(lowered) for p in _TOOL_SIGNAL_PATTERNS)


class ClassifyMixin:
    """Contract extraction + intent classification for the planning scheduler."""

    async def _resolve_contracts(self, run_id: str, intent: str) -> None:
        """S07 (D-02 / 方案 B): 首轮 plan 前的契约解析（run 内异步前置）。

        API 层不再在 HTTP 请求内同步等待抽取（不再阻塞 create_run 响应）。
        本方法在 scheduler 后台生命周期内强制执行契约解析，保证完成门在
        plan/execute 前拿到 settle 后的契约。超时/失败 → contracts=[] +
        unverified（D-04 兜底，不阻断 Run）。
        """
        state = await self._refresh_state(run_id)
        if not state.requires_contract_extraction:
            return
        if self.planner.llm is None or self.planner.registry is None:
            _sched_ctrl.info("[extract] LLM/registry unavailable — skipping extraction (D-04)")
            return
        from harness.core.contract_extractor import CONTRACT_EXTRACT_TIMEOUT, ContractExtractor

        extractor = ContractExtractor(self.planner.llm, self.planner.registry)
        remaining = self._run_remaining_s(run_id)
        cap = CONTRACT_EXTRACT_TIMEOUT if remaining is None else min(CONTRACT_EXTRACT_TIMEOUT, remaining)
        try:
            extracted = await asyncio.wait_for(extractor.extract(intent), timeout=cap)
            await self._append_run_event(
                run_id,
                EventType.DELIVERY_CONTRACTS_RESOLVED,
                DeliveryContractsResolvedPayload(
                    contracts=extracted,
                    source="extracted",
                ).model_dump(),
            )
        except asyncio.TimeoutError:
            _sched_ctrl.warning("[extract] Contract extraction timed out for run=%s — unverified (D-04)", run_id)
            await self._append_run_event(
                run_id,
                EventType.DELIVERY_CONTRACTS_RESOLVED,
                DeliveryContractsResolvedPayload(
                    contracts=[],
                    source="extracted",
                    timed_out=True,
                    error="contract extraction timed out",
                ).model_dump(),
            )
        except Exception as exc:
            _sched_think.warning("[extract] Contract extraction failed for run=%s: %s — unverified (D-04)", run_id, exc)
            await self._append_run_event(
                run_id,
                EventType.DELIVERY_CONTRACTS_RESOLVED,
                DeliveryContractsResolvedPayload(
                    contracts=[],
                    source="extracted",
                    error=repr(exc),
                ).model_dump(),
            )

    async def _classify_intent(self, run_id: str, intent: str) -> bool:
        """Return True if the intent needs external tools, False if analysis-only.

        Bug JAGENT-2026-P1-13: LLM classify='no' 曾绕过整个 Tool Layer/Guardrail
        （文件写入请求被当成分析请求）。受信保守门先于 LLM 判定：
        若意图含确定性工具操作信号（文件/路径/URL/浏览器/workspace），
        直接返回 needs_tools=True，LLM 的 "no" 不再生效。
        """
        truncated = intent[:500] if len(intent) > 500 else intent
        if intent_requires_tools(truncated):
            _sched_ctrl.info(
                "[classify] TRUSTED GATE forced needs_tools=True (tool signal detected): %s",
                truncated[:80],
            )
            return True

        prompt = get_prompt(AgentPhase.CLASSIFY, intent=truncated)
        _sched_ctrl.debug(
            "[classify] phase=%s current_request=%s context_len=0",
            AgentPhase.CLASSIFY.value,
            truncated[:80],
        )
        try:
            _t0 = time.monotonic()
            chat_resp = await self._phase_call(
                run_id,
                "classify",
                self.planner.llm.chat(
                    [{"role": "system", "content": prompt}],
                    temperature=0.0,
                    max_tokens=4,
                    run_id=run_id,
                ),
            )
            _sched_ctrl.info(
                "[llm] phase=classify run=%s duration_ms=%d chars=%d",
                run_id,
                int((time.monotonic() - _t0) * 1000),
                len(chat_resp.content) if chat_resp and chat_resp.content else 0,
            )
        except asyncio.TimeoutError:
            _sched_think.warning("[classify] Phase timed out — assuming needs_tools=True (conservative)")
            return True
        except Exception as exc:
            _sched_think.warning("[classify] LLM call failed: %s — assuming needs_tools=True", exc)
            return True
        result = chat_resp.content.strip().lower()
        needs = result != "no"
        _sched_ctrl.info(
            "[classify] current_request=%s needs_tools=%s raw=%s",
            truncated[:80],
            needs,
            result[:20],
        )
        return needs
