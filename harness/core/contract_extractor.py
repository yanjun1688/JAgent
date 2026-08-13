"""Contract extraction (S07, D-02 方案 B) — intent → DeliveryContract 抽取兜底。

抽取步骤独立于规划（Review 方案 B），使用固定 schema 与独立 prompt。该调用
是**非受信** LLM 输出：结果必须经受信结构校验（tool 存在 + input 为 dict +
含操作判别键），无效项丢弃；全部无效或抽取失败 → 返回空列表（D-04 兜底：
contracts=[] + 全局 unverified，不阻断 Run）。
"""

from __future__ import annotations

import json

from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger, guard_logger
from harness.models.intent import DeliveryContract, DeliverySource, validate_delivery_contract_input
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


class ContractExtractor:
    """Non-trusted extractor with trusted structural validation gate."""

    def __init__(self, llm_client: LLMClient, registry: ToolRegistry, max_retries: int = 1):
        self.llm = llm_client
        self.registry = registry
        self.max_retries = max_retries

    def _build_prompt(self, intent: str) -> str:
        tool_names = ", ".join(sorted(self.registry.tool_names))
        return _EXTRACT_PROMPT.format(intent=intent[:4000], tool_names=tool_names)

    def _validate(self, item: dict) -> DeliveryContract | None:
        """受信结构校验：tool 存在 + input 为 dict + 含操作判别键。无效 → None。"""
        tool = item.get("tool", "")
        op_input = item.get("input")
        if not isinstance(tool, str) or not tool:
            return None
        if self.registry.get_tool_def(tool) is None:
            _guard.warning("[extract] Dropping contract for unknown tool '%s'", tool)
            return None
        if not isinstance(op_input, dict):
            _guard.warning("[extract] Dropping contract for tool '%s': input not an object", tool)
            return None
        errors = validate_delivery_contract_input(tool, op_input, self.registry.get_tool_def(tool))
        if errors:
            _guard.warning("[extract] Dropping contract for tool '%s': %s", tool, "; ".join(errors))
            return None
        return DeliveryContract(tool=tool, input=op_input, source=DeliverySource.EXTRACTED)

    def _parse(self, response: str) -> list[DeliveryContract]:
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
                _guard.warning("[extract] No JSON object found in extraction response")
                return []
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                _guard.warning("[extract] Extraction JSON parse failed")
                return []
        if not isinstance(data, dict):
            return []
        raw_ops = data.get("required_operations")
        if not isinstance(raw_ops, list):
            return []
        contracts: list[DeliveryContract] = []
        for item in raw_ops:
            if not isinstance(item, dict):
                continue
            contract = self._validate(item)
            if contract is not None:
                contracts.append(contract)
        return contracts

    async def extract(self, intent: str) -> list[DeliveryContract]:
        """抽取兜底：intent → contracts（source=extracted）。

        抽取失败或全部无效 → []（D-04：contracts=[] + unverified，不阻断 Run）。
        """
        if not intent:
            return []
        prompt = self._build_prompt(intent)
        last_err = ""
        for attempt in range(self.max_retries + 1):
            try:
                chat_resp = await self.llm.chat(
                    [{"role": "system", "content": prompt}],
                    temperature=0.0,
                )
            except Exception as exc:
                last_err = repr(exc)
                _guard.warning(
                    "[extract] LLM call failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    last_err,
                )
                continue
            contracts = self._parse(chat_resp.content)
            _log.info(
                "[extract] intent=%.60s → %d contract(s) validated",
                intent[:60],
                len(contracts),
            )
            return contracts
        _guard.warning("[extract] All attempts failed (%s) — returning empty contracts (D-04)", last_err)
        return []
