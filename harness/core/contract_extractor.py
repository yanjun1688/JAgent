"""Contract extraction (S07, D-02 方案 B) — intent → DeliveryContract 抽取兜底。

抽取步骤独立于规划（Review 方案 B），使用固定 schema 与独立 prompt。该调用
是**非受信** LLM 输出：结果必须经受信结构校验（tool 存在 + input 为 dict +
含操作判别键），无效项丢弃；JSON 解析失败或列表内全部无效 → 反馈给模型重试
（对齐 Planner.plan/revise 语义）。重试预算用尽或全部无效仍为空 → 返回空列表
（D-04 兜底：contracts=[] + 全局 unverified，不阻断 Run）。
"""

from __future__ import annotations

import json

from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger, guard_logger
from harness.models.intent import DeliveryContract, DeliverySource, validate_delivery_contract_input
from harness.models.tools import unknown_tool_message
from harness.tools.registry import ToolRegistry

_log = agent_logger("contract_extractor")
_guard = guard_logger("contract_extractor")

# 契约抽取单次调用超时（秒）。抽取在 scheduler 首轮 plan 前（run 内）执行，
# 不再占用 API 请求时间；该上限约束 run 内的等待。抽取超时 → contracts=[]
# + unverified（D-04 兜底，不阻断 Run）。
CONTRACT_EXTRACT_TIMEOUT = 15.0

_EXTRACT_PROMPT = """\
Extract the REQUIRED hard-delivery operations from the user's request as JSON.

A required operation is something the user EXPLICITLY asked to be performed —
creating/writing/deleting/reading files, fetching URLs, navigating browsers,
querying external services. Do NOT extract soft suggestions or hypothetical
operations ("if it fails", "maybe", "optional").

Return ONLY valid JSON with this exact shape:
{{"required_operations": [{{"tool": "<tool_name>", "input": {{<key>: <value>}}}}]}}

Rules:
- tool must be one of: {tool_names}
- input must include the discriminating key for that tool:
    file_op      → "operation" (one of read/write/append/delete/list) + "path"
    http_request → "method" (GET/POST/PUT/PATCH/DELETE/HEAD) + "url"
    browser      → "action" (navigate/click/type/extract/screenshot)
    mcp_call     → "tool_name"
- include "content" for file_op write/append if the user specified it
- include "path" exactly as the user wrote it — never rewrite it
- if there are no hard delivery requirements, return {{"required_operations": []}}

User request:
{intent}
"""

# 解析/校验失败时的重试反馈（对齐 Planner.plan/revise 的 retry_prompt 语义）：
# 错误塞回模型 → 下一轮只修正格式/工具名，不重复正确内容。
_RETRY_HINT = (
    "Your previous extraction response had a format or validation error:\n{error}\n\n"
    "Re-respond with ONLY valid JSON of this exact shape: "
    '{{"required_operations": [{{"tool": "<tool_name>", "input": {{<key>: <value>}}}}]}}\n'
    "Available tools: {tool_names}\n"
    "If the request truly has no hard delivery requirement, "
    'return {{"required_operations": []}}.'
)


class ContractExtractor:
    """Non-trusted extractor with trusted structural validation gate."""

    def __init__(self, llm_client: LLMClient, registry: ToolRegistry, max_retries: int = 1):
        self.llm = llm_client
        self.registry = registry
        self.max_retries = max_retries

    def _build_prompt(self, intent: str) -> str:
        tool_names = ", ".join(sorted(self.registry.tool_names))
        return _EXTRACT_PROMPT.format(intent=intent[:4000], tool_names=tool_names)

    def _validate(self, item: dict) -> tuple[DeliveryContract | None, str]:
        """受信结构校验：tool 存在 + input 为 dict + 含操作判别键。

        Returns (contract, "") on success; (None, reason) when dropped.
        """
        tool = item.get("tool", "")
        op_input = item.get("input")
        if not isinstance(tool, str) or not tool:
            return None, "operation missing a 'tool' name"
        if self.registry.get_tool_def(tool) is None:
            reason = unknown_tool_message(tool)
            _guard.warning("[extract] Dropping contract for %s", reason)
            return None, reason
        if not isinstance(op_input, dict):
            reason = f"tool '{tool}': input not an object"
            _guard.warning("[extract] Dropping contract for %s", reason)
            return None, reason
        errors = validate_delivery_contract_input(tool, op_input, self.registry.get_tool_def(tool))
        if errors:
            reason = f"tool '{tool}': {'; '.join(errors)}"
            _guard.warning("[extract] Dropping contract for %s", reason)
            return None, reason
        return DeliveryContract(tool=tool, input=op_input, source=DeliverySource.EXTRACTED), ""

    def _parse(self, response: str) -> tuple[list[DeliveryContract], str]:
        """Parse + structural validation.

        Returns (contracts, "") when the response is *accepted* — this covers
        a valid non-empty result, a partial result, and the legitimate empty
        answer ``{"required_operations": []}``. Returns ([], error) when the
        response is malformed / rejected and should be retried with ``error``
        fed back to the model (mirrors Planner.parse_plan_response).
        """
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end <= start:
                return [], "no JSON object found in extraction response"
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return [], "extraction JSON parse failed"
        if not isinstance(data, dict):
            return [], "response JSON root is not an object"
        raw_ops = data.get("required_operations")
        if raw_ops is None:
            return [], "missing 'required_operations' key"
        if not isinstance(raw_ops, list):
            return [], "'required_operations' is not a list"
        contracts: list[DeliveryContract] = []
        dropped: list[str] = []
        for item in raw_ops:
            if not isinstance(item, dict):
                dropped.append("a listed operation was not an object")
                continue
            contract, reason = self._validate(item)
            if contract is not None:
                contracts.append(contract)
            else:
                dropped.append(reason)
        if contracts or not dropped:
            # 部分有效，或 raw_ops 为空（合法空 = 无硬性交付）
            return contracts, ""
        return [], "all listed operations were rejected: " + " | ".join(dropped)

    async def extract(self, intent: str) -> list[DeliveryContract]:
        """抽取兜底：intent → contracts（source=extracted）。

        对齐 Planner.plan/revise 的重试语义：LLM 调用异常、JSON 解析失败、
        列表内全部被结构校验丢弃 → 反馈给模型重试；合法空（无硬性交付）立即
        返回 []；部分有效返回有效子集。预算用尽或空 intent → []（D-04：
        contracts=[] + unverified，不阻断 Run）。
        """
        if not intent:
            return []
        prompt = self._build_prompt(intent)
        tool_names = ", ".join(sorted(self.registry.tool_names))
        last_err = ""
        total = self.max_retries + 1
        for attempt in range(1, total + 1):
            messages = [{"role": "system", "content": prompt}]
            if last_err:
                messages.append(
                    {"role": "user", "content": _RETRY_HINT.format(error=last_err, tool_names=tool_names)}
                )
            try:
                chat_resp = await self.llm.chat(messages, temperature=0.0)
            except Exception as exc:
                last_err = repr(exc)
                _guard.warning(
                    "[extract] LLM call failed (attempt %d/%d): %s",
                    attempt,
                    total,
                    last_err,
                )
                continue
            contracts, parse_err = self._parse(chat_resp.content)
            if not parse_err:
                _log.info(
                    "[extract] intent=%.60s → %d contract(s) validated",
                    intent[:60],
                    len(contracts),
                )
                return contracts
            last_err = parse_err
            _guard.warning(
                "[extract] Response rejected (attempt %d/%d): %s",
                attempt,
                total,
                last_err,
            )
        _guard.warning("[extract] All attempts failed (%s) — returning empty contracts (D-04)", last_err)
        return []
